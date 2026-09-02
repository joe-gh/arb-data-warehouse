"""Copy/paste of logo assignments: validated like manual saves, journaled."""

import psycopg2

from tests.conftest import TEST_ADMIN_DSN

ROW_P1 = {"option_row": 1, "position": 1, "design_id": "DESIGN-1", "logo_code": "C1",
          "color_scheme_id": "SCHEME-1", "location": "Left Chest", "optional": False,
          "background": "", "cost_override": None, "sort_order": 0, "image_url": "",
          "name_override": "Pasted name", "active": True}
ROW_P2 = {**ROW_P1, "position": 2, "design_id": "DESIGN-2", "logo_code": "C2",
          "color_scheme_id": "SCHEME-2", "location": "Right Chest", "name_override": None}


def _rows(color):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT option_row, position, design_id, name_override FROM logo.assignment "
                "WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' AND garment_color_code=%s "
                "ORDER BY option_row, position", (color,))
            return cursor.fetchall()


def test_paste_channel_onto_other_colors_and_undo(client_as):
    client = client_as()
    response = client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["BLU", "GRN", "NOPE"],
        "rows": [ROW_P2, ROW_P1], "overwrite": False, "as_new_rows": False,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 4 and body["skipped_missing_color"] == 1 and body["skipped_invalid"] == 0
    assert _rows("BLU") == [(1, 1, "DESIGN-1", "Pasted name"), (1, 2, "DESIGN-2", None)]
    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.status_code == 200 and undo.json()["restored"] == 4
    assert _rows("BLU") == [] and _rows("GRN") == []


def test_paste_respects_occupied_slots_unless_overwrite(client_as):
    client = client_as()
    first = client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["RED"], "rows": [ROW_P1],
    }).json()
    assert first["created"] == 0 and first["updated"] == 0 and first["skipped_occupied"] == 1
    assert _rows("RED")[0][3] == "Shopper-facing test name"
    second = client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["RED"], "rows": [ROW_P1], "overwrite": True,
    }).json()
    assert second["updated"] == 1
    assert _rows("RED")[0][3] == "Pasted name"


def test_paste_as_new_rows_appends_after_existing_rows(client_as):
    client = client_as()
    body = client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["RED"],
        "rows": [ROW_P1, ROW_P2], "as_new_rows": True,
    }).json()
    assert body["created"] == 2
    assert [(r[0], r[1]) for r in _rows("RED")] == [(1, 1), (1, 2), (2, 1), (2, 2)]


def test_paste_reports_invalid_rows_instead_of_failing_the_batch(client_as):
    client = client_as()
    bad = {**ROW_P1, "design_id": "NO-SUCH-DESIGN"}
    body = client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["BLU"], "rows": [bad, ROW_P2],
    }).json()
    assert body["created"] == 0 and body["skipped_invalid"] == 2
    assert body["problems"][0]["reason"].startswith("unknown design_id")
    assert _rows("BLU") == []


def test_paste_bounds(client_as):
    client = client_as()
    assert client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-1", "colors": ["BLU"],
        "rows": [ROW_P1, {**ROW_P1}],
    }).status_code == 422  # duplicate (option_row, position)
    assert client.post("/api/assignments/paste", json={
        "store": "S_TEST", "style": "STYLE-9", "colors": ["BLU"], "rows": [ROW_P1],
    }).status_code == 404


# ---- Task C3: batch endpoints ----

def test_paste_batch_matching_color_across_styles(client_as):
    client = client_as()
    response = client.post("/api/assignments/paste-batch", json={
        "store": "S_TEST", "styles": ["STYLE-1", "STYLE-2", "NOPE"],
        "color_scope": "match", "match_color": "BLU", "rows": [ROW_P1],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    by_style = {r["style"]: r for r in body["results"]}
    assert by_style["STYLE-1"]["created"] == 1                # BLU exists on STYLE-1
    assert by_style["STYLE-2"]["skipped_missing_color"] == 1  # STYLE-2 only has RED
    assert by_style["NOPE"]["error"] == "Target style not found"
    assert body["totals"]["created"] == 1
    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.json()["restored"] == 1


def test_paste_batch_all_colors_is_bounded(client_as):
    client = client_as()
    body = client.post("/api/assignments/paste-batch", json={
        "store": "S_TEST", "styles": ["STYLE-2"], "color_scope": "all", "rows": [ROW_P1],
    }).json()
    assert body["results"][0]["colors"] == ["RED"] and body["results"][0]["created"] == 1


def test_style_active_batch(client_as):
    client = client_as()
    response = client.post("/api/style-active-batch", json={
        "store": "S_TEST", "styles": ["STYLE-1", "STYLE-2"], "active": False,
    })
    assert response.status_code == 200, response.text
    results = {r["style"]: r for r in response.json()["results"]}
    assert results["STYLE-1"]["updated"] == 2
    assert results["STYLE-2"]["updated"] == 0


# ---- Task C5: copy one style's logos to many styles ----

def _rows_for(style, color):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT option_row, position, design_id, name_override FROM logo.assignment "
                "WHERE fdm4_store='S_TEST' AND product_style=%s AND garment_color_code=%s "
                "ORDER BY option_row, position", (style, color))
            return cursor.fetchall()


def _seed_variation(style, color, name):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO woo.store_product_state
                    (fdm4_store, catalog_id, sku, kind, style_code, parent_sku, name, status,
                     color_code, color, size_code, size, price, stock, payload, content_hash, is_active)
                VALUES ('S_TEST', 'S_TEST_catalog', %s, 'variation', %s, %s, %s, 'publish',
                        %s, %s, 'M', 'M', 12, 1, '{}'::jsonb, '', true)
                """,
                (f"{style}-{color}", style, style, name, color, name),
            )


def _seed_class(code, name, light_dark):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO logo.color_class (color_code, color_name, light_dark, source)
                VALUES (%s, %s, %s, 'manual')
                ON CONFLICT (color_code) DO UPDATE SET light_dark = EXCLUDED.light_dark
                """,
                (code, name, light_dark),
            )


def test_copy_style_batch_exact_copies_only_shared_colors(client_as):
    client = client_as()
    preview = client.post("/api/copy-style-batch/preview", json={
        "store": "S_TEST", "source_style": "STYLE-1",
        "target_styles": ["STYLE-2", "STYLE-1", "NOPE"], "color_match": "exact",
    })
    assert preview.status_code == 200, preview.text
    targets = {t["style"]: t for t in preview.json()["targets"]}
    assert targets["STYLE-2"]["mappings"] == [
        {"target_color": "RED", "source_color": "RED", "via": "exact", "rows": 2}]
    assert targets["STYLE-2"]["existing"] == 0
    assert targets["STYLE-1"]["error"] == "Source and target styles must differ"
    assert targets["NOPE"]["error"] == "Target style not found"
    assert preview.json()["total_rows"] == 2

    run = client.post("/api/copy-style-batch", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "exact", "mode": "merge",
    })
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["totals"]["created"] == 2
    assert [(r[0], r[1], r[2]) for r in _rows_for("STYLE-2", "RED")] == [(1, 1, "DESIGN-1"), (1, 2, "DESIGN-2")]
    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.json()["restored"] == 2 and _rows_for("STYLE-2", "RED") == []


def test_copy_style_batch_like_maps_by_light_dark_class(client_as):
    _seed_variation("STYLE-2", "NVY", "Navy")
    _seed_class("RED", "Red", "dark")
    _seed_class("NVY", "Navy", "dark")
    client = client_as()
    like = client.post("/api/copy-style-batch/preview", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "like",
    }).json()
    mappings = {m["target_color"]: m for m in like["targets"][0]["mappings"]}
    assert mappings["RED"]["via"] == "exact"
    assert mappings["NVY"] == {"target_color": "NVY", "source_color": "RED", "via": "dark", "rows": 2}
    exact = client.post("/api/copy-style-batch/preview", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "exact",
    }).json()
    assert exact["targets"][0]["unmatched"] == ["NVY"]

    run = client.post("/api/copy-style-batch", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "like", "mode": "merge",
    }).json()
    assert run["totals"]["created"] == 4
    assert [r[2] for r in _rows_for("STYLE-2", "NVY")] == ["DESIGN-1", "DESIGN-2"]


def test_copy_style_batch_overwrite_and_bounds(client_as):
    client = client_as()
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO logo.assignment (fdm4_store, product_style, garment_color_code,
                    option_row, position, design_id, logo_code, color_scheme_id, name_override, updated_by)
                VALUES ('S_TEST','STYLE-2','RED',1,1,'DESIGN-1','C1','SCHEME-1','keep me','seed')
                """)
    merge = client.post("/api/copy-style-batch", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "exact", "mode": "merge",
    }).json()
    assert merge["totals"] == {"created": 1, "updated": 0, "removed": 0, "skipped_occupied": 1, "skipped_invalid": 0}
    assert _rows_for("STYLE-2", "RED")[0][3] == "keep me"
    overwrite = client.post("/api/copy-style-batch", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "exact", "mode": "overwrite",
    }).json()
    assert overwrite["totals"]["updated"] == 2
    assert _rows_for("STYLE-2", "RED")[0][3] == "Shopper-facing test name"
    assert client.post("/api/copy-style-batch", json={
        "store": "S_TEST", "source_style": "STYLE-9", "target_styles": ["STYLE-2"],
    }).status_code == 404


# ---- Task C6: undo restores rows a batch deleted (replace mode) ----

def test_copy_style_batch_replace_clears_then_copies_and_undo_restores(client_as):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO logo.assignment (fdm4_store, product_style, garment_color_code,
                    option_row, position, design_id, logo_code, color_scheme_id, name_override, updated_by)
                VALUES ('S_TEST','STYLE-2','RED',3,1,'DESIGN-2','C2','SCHEME-2','stale','seed')
                """)
    client = client_as()
    run = client.post("/api/copy-style-batch", json={
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "exact", "mode": "replace",
    }).json()
    assert run["totals"]["removed"] == 1 and run["totals"]["created"] == 2
    assert [(r[0], r[1]) for r in _rows_for("STYLE-2", "RED")] == [(1, 1), (1, 2)]
    undo = client.post("/api/bulk-apply/undo", json={"batch_id": run["batch_id"]}).json()
    assert undo["restored"] == 3 and undo["skipped"] == 0
    assert [(r[0], r[1], r[3]) for r in _rows_for("STYLE-2", "RED")] == [(3, 1, "stale")]


# ---- Task C7: replace a design across a store ----

SWAP_1_TO_2 = {"store": "S_TEST", "from_design_id": "DESIGN-1", "from_color_scheme_id": "SCHEME-1",
               "to_design_id": "DESIGN-2", "to_color_scheme_id": "SCHEME-2"}


def _swap_rows(color="RED"):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT option_row, position, design_id, logo_code, color_scheme_id, image_url, "
                "name_override, updated_by FROM logo.assignment "
                "WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' AND garment_color_code=%s "
                "ORDER BY option_row, position", (color,))
            return cursor.fetchall()


def _batch_target(batch_id):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT target, applied FROM logo.bulk_batch WHERE batch_id=%s", (batch_id,))
            return cursor.fetchone()


def test_design_swap_preview_lists_rows_and_derives_the_logo_code(client_as):
    client = client_as()
    response = client.post("/api/design-swap/preview", json=SWAP_1_TO_2)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"] == {"design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2",
                              "logo_code": "C2", "logo_code_derived": True}
    assert body["styles"] == ["STYLE-1"]
    assert body["counts"] == {"total": 1, "ok": 1, "unchanged": 0, "invalid": 0, "styles": 1,
                              "image_url_replaced": 0, "image_url_cleared": 0}
    [group] = body["groups"]
    assert (group["style"], group["color"]) == ("STYLE-1", "RED")
    [row] = group["rows"]
    assert (row["option_row"], row["position"], row["verdict"], row["image_action"]) == (1, 1, "ok", "none")
    assert row["was"] == {"design_id": "DESIGN-1", "logo_code": "C1", "color_scheme_id": "SCHEME-1"}
    assert _swap_rows()[0][2] == "DESIGN-1"  # preview writes nothing
    narrowed = client.post("/api/design-swap/preview", json={**SWAP_1_TO_2, "styles": ["STYLE-2"]}).json()
    assert narrowed["counts"]["total"] == 0 and narrowed["groups"] == []
    same = client.post("/api/design-swap/preview", json={
        "store": "S_TEST", "from_design_id": "DESIGN-2", "from_color_scheme_id": None,
        "to_design_id": "DESIGN-2", "to_color_scheme_id": "SCHEME-2",
    }).json()
    assert same["counts"]["unchanged"] == 1 and same["counts"]["ok"] == 0


def test_design_swap_executes_journals_and_undoes(client_as):
    client = client_as()
    any_scheme = {"store": "S_TEST", "from_design_id": "DESIGN-1", "from_color_scheme_id": None,
                  "to_design_id": "B9H-TEST-DESIGN", "to_color_scheme_id": "WH"}
    response = client.post("/api/design-swap", json=any_scheme)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 1 and body["skipped_invalid"] == 0 and body["target"]["logo_code"] == "B9H"
    assert _swap_rows() == [
        (1, 1, "B9H-TEST-DESIGN", "B9H", "WH", "", "Shopper-facing test name", "admin-one"),
        (1, 2, "DESIGN-2", "C2", "SCHEME-2", "", None, "seed"),
    ]
    target, applied = _batch_target(body["batch_id"])
    assert target["kind"] == "design_swap" and target["to"]["design_id"] == "B9H-TEST-DESIGN" and applied == 1
    assert client.post("/api/design-swap", json=any_scheme).status_code == 404  # nothing left on DESIGN-1
    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["restored"] == 1
    assert _swap_rows()[0][2:5] == ("DESIGN-1", "C1", "SCHEME-1")
    assert _swap_rows()[0][7] == "seed"


def test_design_swap_rejects_targets_the_store_cannot_use(client_as):
    client = client_as()
    foreign = {"store": "S_TEST", "from_design_id": "DESIGN-1",
               "to_design_id": "ART-9001", "to_color_scheme_id": "SCHEME-1"}
    preview = client.post("/api/design-swap/preview", json=foreign)
    assert preview.status_code == 422 and "different FDM4 customer" in preview.json()["detail"]
    assert client.post("/api/design-swap", json=foreign).status_code == 422
    unknown = client.post("/api/design-swap/preview", json={**foreign, "to_design_id": "NO-SUCH-DESIGN"})
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown design_id NO-SUCH-DESIGN"
    no_art = client.post("/api/design-swap/preview",
                         json={**foreign, "to_design_id": "DESIGN-2", "to_color_scheme_id": "NOPE"})
    assert no_art.status_code == 422 and no_art.json()["detail"].startswith("to_logo_code is required")
    # an explicit code is validated per row exactly like a manual save
    explicit = client.post("/api/design-swap/preview", json={
        **foreign, "to_design_id": "DESIGN-2", "to_color_scheme_id": "NOPE", "to_logo_code": "C2"}).json()
    assert explicit["counts"]["invalid"] == 1
    assert explicit["groups"][0]["rows"][0]["reason"] == "design DESIGN-2 has no color scheme NOPE"
    nothing = client.post("/api/design-swap", json={
        **foreign, "to_design_id": "DESIGN-2", "to_color_scheme_id": "NOPE", "to_logo_code": "C2"})
    assert nothing.status_code == 422 and nothing.json()["detail"].startswith("Nothing to swap")
    assert _swap_rows()[0][2] == "DESIGN-1"


def test_design_swap_requires_a_logo_code_when_art_is_ambiguous(client_as):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO fdm4.cust_art_file (art_id, color_scheme_id, resource_type, "
                "target_web_path, target_filename) VALUES ('DESIGN-2', 'SCHEME-2', 'THUMB', "
                "'test/design-2-alt.png', 'C2ALT_SCHEME-2.png')")
    client = client_as()
    ambiguous = client.post("/api/design-swap/preview", json=SWAP_1_TO_2)
    assert ambiguous.status_code == 422
    assert ambiguous.json()["detail"] == (
        "to_logo_code is required: design DESIGN-2 / scheme SCHEME-2 has 2 logo codes on file (C2, C2ALT)")
    explicit = client.post("/api/design-swap/preview", json={**SWAP_1_TO_2, "to_logo_code": "c2alt"}).json()
    assert explicit["target"]["logo_code"] == "C2ALT" and explicit["target"]["logo_code_derived"] is False
    assert explicit["counts"]["ok"] == 1


def test_design_swap_replaces_or_clears_the_storefront_image(client_as):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE logo.assignment SET image_url='https://img.test/old.png' "
                           "WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' "
                           "AND garment_color_code='RED' AND option_row=1 AND position=1")
            cursor.execute("UPDATE logo.assignment SET image_url='https://img.test/new.png' "
                           "WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' "
                           "AND garment_color_code='RED' AND option_row=1 AND position=2")
    client = client_as()
    replaced = client.post("/api/design-swap/preview", json=SWAP_1_TO_2).json()
    assert replaced["image_url_replacement"] == "https://img.test/new.png"
    assert replaced["groups"][0]["rows"][0]["image_action"] == "replaced"
    assert replaced["counts"]["image_url_replaced"] == 1
    cleared = client.post("/api/design-swap/preview", json={
        **SWAP_1_TO_2, "to_design_id": "B9H-TEST-DESIGN", "to_color_scheme_id": "WH"}).json()
    assert cleared["groups"][0]["rows"][0]["image_action"] == "cleared"
    assert cleared["counts"]["image_url_cleared"] == 1
    body = client.post("/api/design-swap", json=SWAP_1_TO_2).json()
    assert _swap_rows()[0][5] == "https://img.test/new.png"
    client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert _swap_rows()[0][5] == "https://img.test/old.png"


def test_design_swap_is_bounded_before_writing(client_as, monkeypatch):
    import mutations
    monkeypatch.setattr(mutations, "MAX_ASSIGNMENT_MUTATION_ROWS", 0)
    client = client_as()
    response = client.post("/api/design-swap", json=SWAP_1_TO_2)
    assert response.status_code == 422 and "mutation limit" in response.json()["detail"]
    assert _swap_rows()[0][2] == "DESIGN-1"
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM logo.bulk_batch")
            assert cursor.fetchone()[0] == 0


def test_dashboard_has_design_swap_dialog(client_as):
    html = client_as().get("/").text
    assert 'id="design-swap-dialog"' in html and 'id="design-swap-open"' in html
