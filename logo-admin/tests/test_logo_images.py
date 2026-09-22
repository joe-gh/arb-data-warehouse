"""Per-store logo images: list, set (rewrites the store's rows, undoable),
clear, and the pick-time / read-side fallbacks."""

import psycopg2

from config import get_settings
from db import database
import mutations
from tests.conftest import TEST_ADMIN_DSN

STORE = "S_TEST"
MEDIA = get_settings().media_base   # must equal the harness MEDIA_BASE


def _admin(sql, params=()):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else None


def _seed_rows():
    # Two styles carry DESIGN-2/SCHEME-2 with DIFFERENT images (the "mapped
    # wrong" case); one inactive row; one row of another design untouched.
    _admin("UPDATE logo.assignment SET image_url = %s WHERE product_style='STYLE-1' AND position = 2",
           (MEDIA + "old-a.png",))
    _admin("""INSERT INTO logo.assignment (fdm4_store, product_style, garment_color_code, option_row, position,
                design_id, logo_code, color_scheme_id, location, optional, background, cost_override,
                sort_order, image_url, name_override, active, updated_by)
              VALUES ('S_TEST','STYLE-2','RED',1,1,'DESIGN-2','C2','SCHEME-2','Right Chest',false,'',NULL,0,%s,NULL,true,'fixture'),
                     ('S_TEST','STYLE-1','BLU',1,1,'DESIGN-2','C2','SCHEME-2','Right Chest',false,'',NULL,0,%s,NULL,false,'fixture')""",
           (MEDIA + "old-b.png", MEDIA + "old-a.png"))


def _card(client, design="DESIGN-2", scheme="SCHEME-2", **params):
    body = client.get("/api/logo-images", params={"store": STORE, **params}).json()
    rows = [r for r in body["rows"] if r["design_id"] == design and r["color_scheme_id"] == scheme]
    assert len(rows) == 1, body
    return rows[0], body


def test_list_reports_rows_styles_mixed_and_options(client_as):
    _seed_rows()
    card, body = _card(client_as())
    assert body["store"] == STORE and body["total"] >= 2
    assert card["rows"] == 2 and card["styles"] == 2 and card["inactive_rows"] == 1
    assert card["mixed"] is True and card["locked"] is False and card["store_image"] is None
    assert card["current_image"] in (MEDIA + "old-a.png", MEDIA + "old-b.png")
    assert {i["url"] for i in card["row_images"]} == {MEDIA + "old-a.png", MEDIA + "old-b.png"}
    assert card["art_options"] and card["art_options"][0]["url"].endswith("test/design-2.png")
    assert card["logo_codes"] == ["C2"]


def test_list_filters(client_as):
    _seed_rows()
    client = client_as()
    mixed = client.get("/api/logo-images", params={"store": STORE, "filter": "mixed"}).json()
    assert {(r["design_id"], r["color_scheme_id"]) for r in mixed["rows"]} == {("DESIGN-2", "SCHEME-2")}
    found = client.get("/api/logo-images", params={"store": STORE, "q": "C1"}).json()
    assert {r["design_id"] for r in found["rows"]} == {"DESIGN-1"}
    assert client.get("/api/logo-images", params={"store": "S_NOPE"}).status_code == 404


def test_list_unset_filter_and_name_search_and_bad_filter(client_as):
    _seed_rows()
    client = client_as()
    # Every card is "unset" until a store image exists.
    unset = client.get("/api/logo-images", params={"store": STORE, "filter": "unset"}).json()
    assert {(r["design_id"], r["color_scheme_id"]) for r in unset["rows"]} >= {("DESIGN-1", "SCHEME-1"), ("DESIGN-2", "SCHEME-2")}
    # A store image on DESIGN-2 removes it from the unset list.
    _admin("INSERT INTO logo.design_image (design_id, color_scheme_id, fdm4_store, image_url, source) VALUES ('DESIGN-2', 'SCHEME-2', %s, %s, 'upload')", (STORE, MEDIA + "set.png"))
    unset = client.get("/api/logo-images", params={"store": STORE, "filter": "unset"}).json()
    assert ("DESIGN-2", "SCHEME-2") not in {(r["design_id"], r["color_scheme_id"]) for r in unset["rows"]}
    card, _ = _card(client)
    assert card["locked"] is True and card["current_image"] == MEDIA + "set.png"
    # Name search uses the store-scoped display name (seed: DESIGN-1/SCHEME-1 = "Store test logo").
    named = client.get("/api/logo-images", params={"store": STORE, "q": "Store test"}).json()
    assert {r["design_id"] for r in named["rows"]} == {"DESIGN-1"}
    assert named["rows"][0]["name"] == "Store test logo"
    assert client.get("/api/logo-images", params={"store": STORE, "filter": "bogus"}).status_code == 422


def _images(style, color):
    return _admin("SELECT position, image_url, active FROM logo.assignment WHERE fdm4_store='S_TEST' "
                  "AND product_style=%s AND garment_color_code=%s ORDER BY position", (style, color))


def test_set_rewrites_every_matching_row_and_undo_restores(client_as):
    _seed_rows()
    client = client_as()
    new = MEDIA + "fixed.png"
    response = client.put("/api/logo-images", json={
        "fdm4_store": STORE, "design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2",
        "image_url": new, "source": "upload",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_rows"] == 3 and body["unchanged_rows"] == 0 and body["styles"] == 2
    assert _images("STYLE-1", "RED")[1][1] == new          # position 2 = DESIGN-2
    assert _images("STYLE-2", "RED")[0][1] == new
    assert _images("STYLE-1", "BLU")[0][1] == new and _images("STYLE-1", "BLU")[0][2] is False
    assert _images("STYLE-1", "RED")[0][1] == ""           # DESIGN-1 row untouched
    card, _ = _card(client)
    assert card["locked"] is True and card["store_image"] == new and card["mixed"] is False
    assert _admin("SELECT count(*) FROM logo.audit_log WHERE action='logo_image_created' AND fdm4_store='S_TEST'")[0][0] == 1

    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.status_code == 200 and undo.json()["restored"] == 3
    assert _images("STYLE-1", "RED")[1][1] == MEDIA + "old-a.png"
    assert _images("STYLE-2", "RED")[0][1] == MEDIA + "old-b.png"
    assert _admin("SELECT count(*) FROM logo.design_image WHERE fdm4_store='S_TEST'")[0][0] == 0


def test_set_twice_undo_restores_previous_store_image(client_as):
    _seed_rows()
    client = client_as()
    first = client.put("/api/logo-images", json={"fdm4_store": STORE, "design_id": "DESIGN-2",
                       "color_scheme_id": "SCHEME-2", "image_url": MEDIA + "one.png", "source": "upload"}).json()
    second = client.put("/api/logo-images", json={"fdm4_store": STORE, "design_id": "DESIGN-2",
                        "color_scheme_id": "SCHEME-2", "image_url": MEDIA + "two.png", "source": "link"}).json()
    assert second["updated_rows"] == 3
    client.post("/api/bulk-apply/undo", json={"batch_id": second["batch_id"]})
    row = _admin("SELECT image_url, source FROM logo.design_image WHERE fdm4_store='S_TEST' AND design_id='DESIGN-2'")
    assert row == [(MEDIA + "one.png", "upload")]
    assert _images("STYLE-2", "RED")[0][1] == MEDIA + "one.png"
    del first


def test_set_validates_url_source_ownership_and_scheme(client_as):
    client = client_as()
    base = {"fdm4_store": STORE, "design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2", "source": "upload"}
    assert client.put("/api/logo-images", json={**base, "image_url": "https://elsewhere.test/x.png"}).status_code == 422
    assert client.put("/api/logo-images", json={**base, "image_url": MEDIA + "x.png", "source": "magic"}).status_code == 422
    assert client.put("/api/logo-images", json={**base, "design_id": "DESIGN-4", "color_scheme_id": "SCHEME-4",
                                                 "image_url": MEDIA + "x.png"}).status_code == 422
    assert client.put("/api/logo-images", json={**base, "color_scheme_id": "NOPE", "image_url": MEDIA + "x.png"}).status_code == 422
    assert client.put("/api/logo-images", json={**base, "fdm4_store": "S_NOPE", "image_url": MEDIA + "x.png"}).status_code == 404
    # A scheme the store does not use yet but FDM4 has art for is allowed (future rows).
    ok = client.put("/api/logo-images", json={"fdm4_store": STORE, "design_id": "DESIGN-3",
                    "color_scheme_id": "SCHEME-3", "image_url": MEDIA + "x.png", "source": "art"})
    assert ok.status_code == 200 and ok.json()["updated_rows"] == 0


def test_clear_removes_store_image_only(client_as):
    _seed_rows()
    client = client_as()
    client.put("/api/logo-images", json={"fdm4_store": STORE, "design_id": "DESIGN-2",
               "color_scheme_id": "SCHEME-2", "image_url": MEDIA + "fixed.png", "source": "upload"})
    response = client.request("DELETE", "/api/logo-images", json={
        "fdm4_store": STORE, "design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2"})
    assert response.status_code == 200, response.text
    assert _admin("SELECT count(*) FROM logo.design_image")[0][0] == 0
    assert _images("STYLE-2", "RED")[0][1] == MEDIA + "fixed.png"   # rows keep the image
    assert client.request("DELETE", "/api/logo-images", json={
        "fdm4_store": STORE, "design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2"}).status_code == 404


class _RecordingCursor:
    """Wraps a real cursor and records every SQL statement (with params) it
    is asked to run, delegating execution unchanged. Used to prove a lock is
    the FIRST statement a mutation issues without depending on pg_locks
    still being held after the call returns."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def test_set_and_clear_lock_the_design_image_key_before_any_read():
    """Finding 1 regression: design_image_styles([]) for a (design, scheme)
    the store has no assignment rows for yet (DESIGN-3/SCHEME-3, the "future
    scheme" case covered by test_set_validates_url_source_ownership_and_scheme)
    means no assignment_style scope is taken, and a plain SELECT ... FOR
    UPDATE on logo.design_image locks nothing when the row is absent. Both
    mutations must take a transaction-scoped advisory lock on the store-image
    key as their very first statement, unconditionally, so two concurrent
    first-time set/clear calls for the same key serialize instead of both
    reading previous=None."""
    key = f"design_image|{STORE}|DESIGN-3|SCHEME-3"
    with database.cursor(write=True, actor="t") as cur:
        rec = _RecordingCursor(cur)
        result = mutations.set_design_image(
            rec, fdm4_store=STORE, design_id="DESIGN-3", color_scheme_id="SCHEME-3",
            image_url=MEDIA + "x.png", source="art", actor="t",
            media_base=get_settings().media_base, art_base=get_settings().fdm4_art_base,
        )
        assert result["updated_rows"] == 0   # no assignment rows for DESIGN-3/SCHEME-3 in S_TEST
        assert rec.calls, "set_design_image issued no statements"
        first_sql, first_params = rec.calls[0]
        assert "pg_advisory_xact_lock" in first_sql
        assert first_params == (key,)

    with database.cursor(write=True, actor="t") as cur:
        rec = _RecordingCursor(cur)
        mutations.clear_design_image(
            rec, fdm4_store=STORE, design_id="DESIGN-3", color_scheme_id="SCHEME-3", actor="t",
        )
        assert rec.calls, "clear_design_image issued no statements"
        first_sql, first_params = rec.calls[0]
        assert "pg_advisory_xact_lock" in first_sql
        assert first_params == (key,)


def test_set_same_url_twice_reports_all_unchanged(client_as):
    _seed_rows()
    client = client_as()
    payload = {"fdm4_store": STORE, "design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2",
               "image_url": MEDIA + "same.png", "source": "upload"}
    first = client.put("/api/logo-images", json=payload).json()
    assert first["updated_rows"] == 3 and first["unchanged_rows"] == 0
    updated_before = _admin(
        "SELECT count(*) FROM logo.audit_log WHERE action='logo_image_updated' AND fdm4_store='S_TEST'")[0][0]

    second = client.put("/api/logo-images", json=payload).json()
    assert second["updated_rows"] == 0 and second["unchanged_rows"] == 3

    updated_after = _admin(
        "SELECT count(*) FROM logo.audit_log WHERE action='logo_image_updated' AND fdm4_store='S_TEST'")[0][0]
    assert updated_after == updated_before   # a true no-op upsert; the audit trigger skips it
    assert _admin("SELECT count(*) FROM logo.design_image WHERE fdm4_store='S_TEST'")[0][0] == 1


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f0005000"
    "1ff9a2b3f0000000049454e44ae426082"
)


def test_fetch_downloads_link_into_storage(client_as, monkeypatch, tmp_path):
    import routes_api
    from config import get_settings
    monkeypatch.setattr(routes_api, "_fetch_legacy_image", lambda url, max_bytes: PNG_1PX)
    client = client_as()
    response = client.post("/api/logo-images/fetch", json={"url": "https://example.test/logo.png"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url"].startswith(get_settings().media_base) and body["url"].endswith(".png")
    assert (get_settings().upload_dir / body["filename"]).read_bytes() == PNG_1PX
    again = client.post("/api/logo-images/fetch", json={"url": "https://example.test/logo.png"}).json()
    assert again["filename"] == body["filename"]     # content-hash naming is idempotent


def test_fetch_rejects_non_images_and_unsafe_urls(client_as, monkeypatch):
    import routes_api
    client = client_as()
    monkeypatch.setattr(routes_api, "_fetch_legacy_image", lambda url, max_bytes: b"<html>")
    assert client.post("/api/logo-images/fetch", json={"url": "https://example.test/x"}).status_code == 422
    monkeypatch.undo()
    # Private addresses never leave the box: the resolver refuses them.
    assert client.post("/api/logo-images/fetch", json={"url": "http://127.0.0.1/x.png"}).status_code == 422


def _set(client, design, scheme, url):
    r = client.put("/api/logo-images", json={"fdm4_store": STORE, "design_id": design,
                   "color_scheme_id": scheme, "image_url": url, "source": "upload"})
    assert r.status_code == 200, r.text
    return r.json()


def test_design_detail_prefers_store_image(client_as):
    # DESIGN-3 / SCHEME-3 has no rows anywhere, so the only candidate picture
    # is the store image: S_TEST sees it, another store sees nothing.
    client = client_as()
    _set(client, "DESIGN-3", "SCHEME-3", MEDIA + "three.png")
    body = client.get("/api/designs/DESIGN-3", params={"store": STORE}).json()
    scheme = [s for s in body["schemes"] if s["color_scheme_id"] == "SCHEME-3"][0]
    assert scheme["warehouse_image_url"] == MEDIA + "three.png"
    other = client.get("/api/designs/DESIGN-3", params={"store": "S_EMPTY"}).json()
    scheme = [s for s in other["schemes"] if s["color_scheme_id"] == "SCHEME-3"][0]
    assert scheme["warehouse_image_url"] == ""   # another store never sees it
    # With rows present, a store's own newest row image still beats other stores'.
    _seed_rows()
    body = client.get("/api/designs/DESIGN-2", params={"store": STORE}).json()
    scheme = [s for s in body["schemes"] if s["color_scheme_id"] == "SCHEME-2"][0]
    assert scheme["warehouse_image_url"] in (MEDIA + "old-a.png", MEDIA + "old-b.png")


def test_bulk_apply_uses_store_image(client_as):
    client = client_as()
    _set(client, "DESIGN-1", "SCHEME-1", MEDIA + "store1.png")
    from db import database
    from mutations import bulk_apply_execute
    with database.cursor(write=True, actor="fixture") as cursor:
        result = bulk_apply_execute(cursor, fdm4_store=STORE, logo_code="C1", color_scheme="SCHEME-1",
                                    placement="Left Chest", rows=[{"style_code": "STYLE-2", "color_code": "RED"}],
                                    actor="fixture", design_id="DESIGN-1")
    assert result["applied"] == 1
    assert _images("STYLE-2", "RED")[0][1] == MEDIA + "store1.png"


def test_design_swap_uses_store_image_for_new_design(client_as):
    _seed_rows()
    client = client_as()
    _set(client, "DESIGN-3", "SCHEME-3", MEDIA + "three.png")
    plan = client.post("/api/design-swap/preview", json={
        "store": STORE, "from_design_id": "DESIGN-2", "from_color_scheme_id": "SCHEME-2",
        "to_design_id": "DESIGN-3", "to_color_scheme_id": "SCHEME-3"}).json()
    assert plan["image_url_replacement"] == MEDIA + "three.png"


def test_undoing_an_older_batch_leaves_a_newer_store_image_alone(client_as):
    """Finding I1: undo is per-batch and can run out of order. Undoing the
    FIRST set after a SECOND one changed the same key already skips every row
    (the journaled after_row no longer matches), so resetting the store image
    would leave logo.design_image disagreeing with the rows it is supposed to
    describe. The store image is only restored when it is still the one that
    batch wrote."""
    _seed_rows()
    client = client_as()
    image_a, image_b = MEDIA + "a.png", MEDIA + "b.png"
    first = _set(client, "DESIGN-2", "SCHEME-2", image_a)
    second = _set(client, "DESIGN-2", "SCHEME-2", image_b)
    assert first["updated_rows"] == 3 and second["updated_rows"] == 3

    undo_first = client.post("/api/bulk-apply/undo", json={"batch_id": first["batch_id"]})
    assert undo_first.status_code == 200, undo_first.text
    body = undo_first.json()
    assert body["restored"] == 0 and body["skipped"] == 3
    assert body["design_image_restored"] is False
    assert _admin("SELECT image_url FROM logo.design_image WHERE fdm4_store='S_TEST'"
                  " AND design_id='DESIGN-2' AND color_scheme_id='SCHEME-2'") == [(image_b,)]
    assert _images("STYLE-2", "RED")[0][1] == image_b
    assert _images("STYLE-1", "RED")[1][1] == image_b

    undo_second = client.post("/api/bulk-apply/undo", json={"batch_id": second["batch_id"]})
    assert undo_second.status_code == 200, undo_second.text
    body = undo_second.json()
    assert body["restored"] == 3 and body["design_image_restored"] is True
    # Batch 2's `previous` is batch 1's row, so both the store image and the
    # assignment rows land back on A - never on the pre-batch-1 images.
    assert _admin("SELECT image_url, source FROM logo.design_image WHERE fdm4_store='S_TEST'"
                  " AND design_id='DESIGN-2' AND color_scheme_id='SCHEME-2'") == [(image_a, "upload")]
    assert _images("STYLE-2", "RED")[0][1] == image_a
    assert _images("STYLE-1", "RED")[1][1] == image_a
