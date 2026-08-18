"""Regression coverage for the validated 2026-08-01 audit fixes."""

import base64

from config import get_settings
from db import database
from design_resolver import load_design_index
import routes_api


def _assignment():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT * FROM logo.assignment
             WHERE fdm4_store = 'S_TEST' AND product_style = 'STYLE-1'
               AND garment_color_code = 'RED' AND option_row = 1
               AND position = 1
            """
        )
        return dict(cursor.fetchone())


def _body(row, **changes):
    body = {
        key: row[key]
        for key in (
            "fdm4_store", "product_style", "garment_color_code", "position",
            "option_row", "design_id", "logo_code", "color_scheme_id",
            "location", "optional", "background", "sort_order", "image_url",
            "active",
        )
    }
    body["cost_override"] = (
        None if row["cost_override"] is None else str(row["cost_override"])
    )
    body.update(changes)
    return body


def test_modern_design_art_mapping_beats_colliding_design_id():
    with database.cursor() as cursor:
        index = load_design_index(cursor)
    assert index.candidates("S_TEST", "C1", "SCHEME-1") == {"DESIGN-1"}
    assert "ART-9001" not in index.candidates("S_TEST", "C1", "SCHEME-1")


def test_assignment_name_override_three_state_and_revision(client_as):
    client = client_as("admin-one")
    original = _assignment()
    assert original["name_override"] == "Shopper-facing test name"

    preserved = client.put(
        "/api/assignments",
        json=_body(
            original,
            location="CENTER CHEST",
            expected_updated_at=original["updated_at"].isoformat(),
        ),
    )
    assert preserved.status_code == 200, preserved.text
    saved = preserved.json()["assignment"]
    assert saved["name_override"] == "Shopper-facing test name"

    stale = client.put(
        "/api/assignments",
        json=_body(
            original,
            location="LEFT CHEST",
            expected_updated_at=original["updated_at"].isoformat(),
        ),
    )
    assert stale.status_code == 409

    cleared = client.put(
        "/api/assignments",
        json=_body(
            original,
            location="CENTER CHEST",
            name_override="",
            expected_updated_at=saved["updated_at"],
        ),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["assignment"]["name_override"] == ""


def test_modern_design_without_explicit_image_is_editable(client_as):
    row = _assignment()
    response = client_as("admin-one").put(
        "/api/assignments",
        json=_body(
            row,
            image_url="",
            expected_updated_at=row["updated_at"].isoformat(),
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["assignment"]["design_id"] == "DESIGN-1"


def _bulk_apply(client):
    response = client.post(
        "/api/bulk-apply/execute",
        json={
            "fdm4_store": "S_TEST",
            "logo_code": "B9H",
            "color_scheme": "WH",
            "placement": "CENTER CHEST",
            "rows": [{"style_code": "STYLE-1", "color_code": "RED"}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["batch_id"]


def test_bulk_undo_restores_name_override_and_history(client_as):
    client = client_as("admin-one")
    batch_id = _bulk_apply(client)
    undone = client.post("/api/bulk-apply/undo", json={"batch_id": batch_id})
    assert undone.status_code == 200, undone.text
    assert undone.json() == {"restored": 1, "skipped": 0, "batch_id": batch_id}
    assert _assignment()["name_override"] == "Shopper-facing test name"

    history = client.get("/api/bulk-apply/batches", params={"store": "S_TEST"})
    assert history.status_code == 200
    batch = history.json()["batches"][0]
    assert batch["batch_id"] == batch_id
    assert batch["undone_at"] is not None


def test_bulk_undo_preserves_a_newer_operator_edit(client_as):
    client = client_as("admin-one")
    batch_id = _bulk_apply(client)
    with database.cursor(write=True, actor="newer-operator") as cursor:
        cursor.execute(
            """
            UPDATE logo.assignment
               SET location = 'RIGHT CHEST', updated_by = 'newer-operator',
                   updated_at = now()
             WHERE fdm4_store = 'S_TEST' AND product_style = 'STYLE-1'
               AND garment_color_code = 'RED' AND option_row = 1
               AND position = 1
            """
        )
    undone = client.post("/api/bulk-apply/undo", json={"batch_id": batch_id})
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored"] == 0
    assert undone.json()["skipped"] == 1
    row = _assignment()
    assert row["location"] == "RIGHT CHEST"
    assert row["updated_by"] == "newer-operator"


def test_logout_revokes_the_server_side_session(client_as):
    client = client_as("admin-one")
    assert client.get("/api/stores").status_code == 200
    response = client.post("/logout", data={"csrf_token": client.csrf})
    assert response.status_code == 200
    assert client.get("/api/stores").status_code == 401


def test_missing_image_mapping_is_redownloaded_and_repaired(
    client_as, monkeypatch
):
    settings = get_settings()
    missing = settings.upload_dir / "missing-fixture.png"
    missing.unlink(missing_ok=True)
    legacy_url = "https://legacy.example.test/logo.png"
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """
            UPDATE logo.assignment SET image_url = %s
             WHERE fdm4_store = 'S_TEST' AND product_style = 'STYLE-1'
               AND garment_color_code = 'RED' AND option_row = 1
               AND position = 1
            """,
            (legacy_url,),
        )
        cursor.execute(
            """
            INSERT INTO logo.image_import (
                source_url, filename, bytes, imported_by
            ) VALUES (%s, 'missing-fixture.png', 123, 'fixture')
            """,
            (legacy_url,),
        )

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "/x8AAusB9Y9Z4aQAAAAASUVORK5CYII="
    )
    monkeypatch.setattr(routes_api, "_fetch_legacy_image", lambda *_args: png)
    response = client_as("admin-one").post(
        "/api/legacy-import-images", data={"limit": "1"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["reused"] == 0
    assert response.json()["downloaded"] == 1
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT filename, bytes FROM logo.image_import WHERE source_url = %s",
            (legacy_url,),
        )
        mapping = cursor.fetchone()
    repaired = settings.upload_dir / mapping["filename"]
    assert mapping["filename"] != "missing-fixture.png"
    assert mapping["bytes"] == len(png)
    assert repaired.is_file()
    repaired.unlink()


def test_sync_records_intent_and_completion(monkeypatch, client_as):
    monkeypatch.setattr(
        routes_api,
        "wordpress_json_request",
        lambda *_args, **_kwargs: {
            "owned": True,
            "reconcile": {"stats": {"updated": 1}},
        },
    )
    response = client_as("admin-one").post(
        "/api/sync", json={"store": "S_TEST", "styles": ["STYLE-1"]}
    )
    assert response.status_code == 200, response.text
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, detail FROM logo.audit_log
             WHERE fdm4_store = 'S_TEST'
               AND action IN ('sync_requested', 'sync_succeeded')
             ORDER BY id
            """
        )
        rows = cursor.fetchall()
    assert [row["action"] for row in rows] == [
        "sync_requested", "sync_succeeded"
    ]
    assert rows[1]["detail"]["intent_id"] is not None
