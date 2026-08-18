"""Rollback previews execute the complete ordered change-set cumulatively."""

from commands import SaveAssignmentCommand, UpdateStoreSettingsCommand
from db import database
from snapshots import canonical_json
from staging import new_change_set, preview_commands, stage_write


def _assignment(cursor):
    cursor.execute(
        "SELECT * FROM logo.assignment "
        "ORDER BY fdm4_store, product_style, garment_color_code, option_row, position LIMIT 1"
    )
    row = cursor.fetchone()
    assert row is not None
    return dict(row)


def _save(row, **changes):
    values = {
        key: row[key]
        for key in (
            "fdm4_store", "product_style", "garment_color_code", "position",
            "option_row", "design_id", "logo_code", "color_scheme_id",
            "location", "optional", "background", "cost_override",
            "sort_order", "image_url", "active",
        )
    }
    values.update(changes)
    return SaveAssignmentCommand.model_validate(values)


def _business_rows():
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(a) AS row FROM logo.assignment a "
            "ORDER BY fdm4_store, product_style, garment_color_code, option_row, position"
        )
        return [dict(row["row"]) for row in cursor.fetchall()]


def _session(user_login="admin-one"):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    session_id = uuid4()
    with database.cursor(write=True, actor=user_login) as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                user_login,
                "preview fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    return session_id


def test_preview_changes_no_business_row():
    before = _business_rows()
    with database.cursor() as cursor:
        row = _assignment(cursor)
    preview = preview_commands(
        [("save_assignment", _save(
            row,
            location="PREVIEW ONLY",
            image_url="https://example.test/preview.png",
        ))],
        "admin-one",
    )
    assert preview.semantic_diff["count"] == 1
    assert preview.results[0]["assignment"]["location"] == "PREVIEW ONLY"
    assert canonical_json(_business_rows()) == canonical_json(before)


def test_two_edits_to_same_row_produce_one_net_change():
    with database.cursor() as cursor:
        row = _assignment(cursor)
    preview = preview_commands(
        [
            ("save_assignment", _save(
                row,
                location="INTERMEDIATE",
                image_url="https://example.test/one.png",
            )),
            ("save_assignment", _save(
                row,
                location="FINAL",
                image_url="https://example.test/two.png",
            )),
        ],
        "admin-one",
    )
    assert preview.semantic_diff["count"] == 1
    change = preview.semantic_diff["changes"][0]
    assert change["after"]["location"] == "FINAL"
    assert change["after"]["image_url"] == "https://example.test/two.png"


def test_companion_can_depend_on_primary_staged_earlier_in_same_preview():
    with database.cursor() as cursor:
        row = _assignment(cursor)
        cursor.execute(
            """
            SELECT COALESCE(max(option_row), 0) + 1 AS next_row
              FROM logo.assignment
             WHERE fdm4_store=%s AND product_style=%s
               AND garment_color_code=%s
            """,
            (row["fdm4_store"], row["product_style"], row["garment_color_code"]),
        )
        option_row = cursor.fetchone()["next_row"]
    assert option_row <= 999
    primary = _save(
        row,
        option_row=option_row,
        position=1,
        image_url="https://example.test/primary.png",
    )
    companion = _save(
        row,
        option_row=option_row,
        position=2,
        image_url="https://example.test/companion.png",
    )
    preview = preview_commands(
        [("save_assignment", primary), ("save_assignment", companion)],
        "admin-one",
    )
    assert preview.semantic_diff["count"] == 2
    assert [result["assignment"]["position"] for result in preview.results] == [1, 2]
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS count FROM logo.assignment
             WHERE fdm4_store=%s AND product_style=%s
               AND garment_color_code=%s AND option_row=%s
            """,
            (row["fdm4_store"], row["product_style"], row["garment_color_code"], option_row),
        )
        assert cursor.fetchone()["count"] == 0


def test_stage_persists_metadata_but_not_previewed_business_change():
    with database.cursor() as cursor:
        row = _assignment(cursor)
    before = _business_rows()
    session_id = _session()
    change_set = new_change_set(session_id, "admin-one")
    staged = stage_write(
        change_set["id"],
        "update_store_settings",
        UpdateStoreSettingsCommand(
            store=row["fdm4_store"],
            enabled=False,
            allows_none=True,
        ).model_dump(mode="json"),
        "call-1",
        "admin-one",
        max_items=50,
    )
    assert staged["revision"] == 1
    assert staged["items"] == 1
    assert len(staged["preview_hash"]) == 64
    assert canonical_json(_business_rows()) == canonical_json(before)
