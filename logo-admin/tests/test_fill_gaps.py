"""Fill missing colors: copy a style's own configured logos onto its
logo-less colors. Preview is read-only; execution is journaled with one
undo batch for the whole run."""

import psycopg2

from tests.conftest import TEST_ADMIN_DSN

ROW_ALT = {"option_row": 1, "position": 1, "design_id": "DESIGN-2", "logo_code": "C2",
           "color_scheme_id": "SCHEME-2", "location": "Right Chest", "optional": False,
           "background": "", "cost_override": None, "sort_order": 0, "image_url": "",
           "name_override": None, "active": True}


def _rows(style, color):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT option_row, position, design_id FROM logo.assignment "
                "WHERE fdm4_store='S_TEST' AND product_style=%s AND garment_color_code=%s "
                "ORDER BY option_row, position", (style, color))
            return cursor.fetchall()


def _entry(body, style):
    matches = [e for e in body["copyable"] if e["style"] == style]
    assert len(matches) == 1, body
    return matches[0]


def test_preview_reports_copyable_and_no_source(client_as):
    client = client_as()
    response = client.post("/api/styles/fill-gaps/preview", json={"store": "S_TEST"})
    assert response.status_code == 200, response.text
    body = response.json()
    entry = _entry(body, "STYLE-1")
    assert entry["targets"] == ["BLU", "GRN"]
    assert entry["auto_source"] == "RED"
    assert entry["needs_choice"] is False
    assert entry["sources"] == [{"color": "RED", "rows": 2}]
    assert entry["slots"] == 4  # 2 rows x 2 target colors
    no_source = {e["style"] for e in body["no_source"]}
    assert "STYLE-2" in no_source


def test_preview_needs_choice_when_configured_colors_differ(client_as):
    client = client_as()
    # Give BLU a DIFFERENT logo set than RED; GRN stays empty.
    paste = client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["BLU"], "rows": [ROW_ALT],
    })
    assert paste.status_code == 200, paste.text
    body = client.post("/api/styles/fill-gaps/preview", json={"store": "S_TEST"}).json()
    entry = _entry(body, "STYLE-1")
    assert entry["targets"] == ["GRN"]
    assert entry["needs_choice"] is True
    assert entry["auto_source"] is None
    assert {s["color"] for s in entry["sources"]} == {"RED", "BLU"}


def test_preview_style_filter(client_as):
    client = client_as()
    body = client.post("/api/styles/fill-gaps/preview",
                       json={"store": "S_TEST", "styles": ["STYLE-2"]}).json()
    assert body["copyable"] == []
    assert [e["style"] for e in body["no_source"]] == ["STYLE-2"]


def test_fill_executes_and_undoes_as_one_batch(client_as):
    client = client_as()
    response = client.post("/api/styles/fill-gaps", json={
        "store": "S_TEST",
        "entries": [{"style": "STYLE-1", "source_color": "RED"}],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 4 and body["updated"] == 0
    result = body["results"][0]
    assert result["style"] == "STYLE-1" and sorted(result["colors"]) == ["BLU", "GRN"]
    assert _rows("STYLE-1", "BLU") == [(1, 1, "DESIGN-1"), (1, 2, "DESIGN-2")]
    assert _rows("STYLE-1", "GRN") == [(1, 1, "DESIGN-1"), (1, 2, "DESIGN-2")]
    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.status_code == 200 and undo.json()["restored"] == 4
    assert _rows("STYLE-1", "BLU") == [] and _rows("STYLE-1", "GRN") == []


def test_fill_respects_explicit_colors_and_occupied_slots(client_as):
    client = client_as()
    # Occupy BLU 1/1 with a different logo first.
    client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["BLU"], "rows": [ROW_ALT],
    })
    response = client.post("/api/styles/fill-gaps", json={
        "store": "S_TEST",
        "entries": [{"style": "STYLE-1", "source_color": "RED", "colors": ["BLU"]}],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # 1/1 occupied -> skipped; 1/2 filled from RED.
    assert body["created"] == 1 and body["results"][0]["skipped_occupied"] == 1
    assert _rows("STYLE-1", "BLU") == [(1, 1, "DESIGN-2"), (1, 2, "DESIGN-2")]
    assert _rows("STYLE-1", "GRN") == []  # explicit colors respected


def test_fill_rejects_source_without_logos(client_as):
    client = client_as()
    response = client.post("/api/styles/fill-gaps", json={
        "store": "S_TEST",
        "entries": [{"style": "STYLE-1", "source_color": "GRN"}],
    })
    assert response.status_code == 422, response.text
    assert _rows("STYLE-1", "BLU") == []


def test_fill_requires_csrf(client_as):
    client = client_as()
    response = client.post("/api/styles/fill-gaps",
                           json={"store": "S_TEST",
                                 "entries": [{"style": "STYLE-1", "source_color": "RED"}]},
                           headers={"X-CSRF-Token": "wrong"})
    assert response.status_code in (401, 403)
    assert _rows("STYLE-1", "BLU") == []


def test_dashboard_renders_bulk_view(client_as):
    client = client_as()
    response = client.get("/")
    assert response.status_code == 200, response.text
    html = response.text
    assert 'id="view-bulk"' in html
    assert 'id="open-bulk-view"' in html
    assert 'data-bulk-job="fill"' in html
    assert 'id="batch-dialog"' not in html  # retired: target picker is inline now


def _admin(statement):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement)


def _seed_over_the_assignment_cap():
    """1,000 gap styles, each with 21 logo rows on its one configured color:
    21,000 assignments, past the plan's 20,000-row bound."""
    _admin("""
        INSERT INTO woo.store_product_state(fdm4_store,catalog_id,sku,kind,style_code,name,payload,content_hash)
        SELECT 'S_TEST','S_TEST_catalog','GAP-'||n,'parent','GAP-'||lpad(n::text,4,'0'),
               repeat('n',4000),'{}','gap' FROM generate_series(1,1000) n""")
    _admin("""
        INSERT INTO woo.store_product_state(fdm4_store,catalog_id,sku,kind,style_code,color_code,color,payload,content_hash)
        SELECT 'S_TEST','S_TEST_catalog','GAP-'||n||'-'||c,'variation','GAP-'||lpad(n::text,4,'0'),
               'C'||c,'Color '||c,'{}','gap' FROM generate_series(1,1000) n, generate_series(0,1) c""")
    _admin("""
        INSERT INTO logo.assignment(fdm4_store,product_style,garment_color_code,option_row,position,
                                    design_id,logo_code,color_scheme_id,location,image_url,name_override)
        SELECT 'S_TEST','GAP-'||lpad(n::text,4,'0'),'C0',r,p,'DESIGN-1','C1','SCHEME-1',
               CASE WHEN n=1 THEN repeat('L',200) ELSE '' END,
               CASE WHEN n=1 THEN repeat('u',2000) ELSE '' END,
               CASE WHEN n=1 THEN repeat('o',200) ELSE NULL END
          FROM generate_series(1,1000) n, generate_series(1,7) r, generate_series(1,3) p""")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_preview_caps_assignment_rows_and_truncates_strings(client_as):
    import queries

    _seed_over_the_assignment_cap()
    client = client_as()
    response = client.post("/api/styles/fill-gaps/preview", json={"store": "S_TEST"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["truncated"] is True
    assert body["truncation"] == {"styles": False, "rows": True, "bytes": False}
    counted = sum(source["rows"] for entry in body["copyable"] for source in entry["sources"])
    assert counted == queries.FILL_GAPS_ASSIGNMENT_LIMIT
    assert max(len(text) for text in _strings(body)) <= queries.READ_TEXT_CHAR_LIMIT


def test_fill_refuses_when_the_style_set_is_past_the_preview_cap(
    client_as,
    monkeypatch,
):
    """The preview stops reading at FILL_GAPS_ASSIGNMENT_LIMIT and says so;
    the executor re-derives its own rows, so it refuses the same size rather
    than acting on colors chosen from a truncated plan."""

    import queries

    monkeypatch.setattr(queries, "FILL_GAPS_ASSIGNMENT_LIMIT", 0)
    client = client_as()
    response = client.post("/api/styles/fill-gaps", json={
        "store": "S_TEST",
        "entries": [{"style": "STYLE-1", "source_color": "RED"}],
    })
    assert response.status_code == 422, response.text
    assert "fewer styles" in response.json()["detail"]
