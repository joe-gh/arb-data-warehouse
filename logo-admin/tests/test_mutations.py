"""Integration contracts for the transaction-only mutation kernel."""

import pytest

from commands import (
    COMMAND_MODELS,
    ApplyToColorsCommand,
    CopyStyleCommand,
    DeactivateAssignmentCommand,
    DeactivateColorCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
    SaveAssignmentCommand,
    SetStorePricingTierCommand,
    SetStyleActiveCommand,
    UpdateStoreSettingsCommand,
)
from db import database
from mutations import (
    MUTATION_HANDLERS,
    apply_to_colors,
    copy_style,
    deactivate_assignment,
    deactivate_color,
    delete_store_pricing_tier,
    hard_delete_assignment,
    hard_delete_color,
    save_assignment,
    set_store_pricing_tier,
    set_style_active,
    update_store_settings,
    MutationResult,
    MutationScope,
)
from domain import InvalidCommand
import staging


def _first_assignment(cursor):
    cursor.execute(
        """
        SELECT * FROM logo.assignment
         ORDER BY fdm4_store, product_style, garment_color_code,
                  option_row, position
         LIMIT 1
        """
    )
    row = cursor.fetchone()
    assert row is not None, "Phase 0 seed must include an assignment"
    return dict(row)


def _primary_with_companion(cursor):
    cursor.execute(
        """
        SELECT fdm4_store, product_style, garment_color_code, option_row
          FROM logo.assignment
         GROUP BY fdm4_store, product_style, garment_color_code, option_row
        HAVING bool_or(position = 1) AND bool_or(position > 1)
         ORDER BY 1, 2, 3, 4
         LIMIT 1
        """
    )
    row = cursor.fetchone()
    assert row is not None, "Phase 0 seed must include a position-1/2 pair"
    return dict(row)


def _save_command(row, **changes):
    values = {
        key: row[key]
        for key in (
            "fdm4_store",
            "product_style",
            "garment_color_code",
            "position",
            "option_row",
            "design_id",
            "logo_code",
            "color_scheme_id",
            "location",
            "optional",
            "background",
            "cost_override",
            "sort_order",
            "image_url",
            "active",
        )
    }
    values.update(changes)
    return SaveAssignmentCommand.model_validate(values)


def test_handler_registry_matches_every_command_model():
    assert set(MUTATION_HANDLERS) == set(COMMAND_MODELS)


def test_save_assignment_preserves_contract_and_attributes_actor():
    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        row = _first_assignment(cursor)
        result = save_assignment(
            cursor,
            "kernel-test",
            _save_command(
                row,
                location="CENTER CHEST",
                image_url="https://example.test/logo.png",
            ),
        )
        assert result.value["ok"] is True
        assert result.value["assignment"]["location"] == "CENTER CHEST"
        assert result.value["assignment"]["updated_by"] == "kernel-test"
        assert result.scopes[0].kind == "assignment_option_row"


def test_deactivating_primary_deactivates_its_complete_option_row():
    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        key = _primary_with_companion(cursor)
        command = DeactivateAssignmentCommand(
            **key,
            position=1,
        )
        deactivate_assignment(cursor, "kernel-test", command)
        cursor.execute(
            """
            SELECT active, updated_by FROM logo.assignment
             WHERE fdm4_store = %(fdm4_store)s
               AND product_style = %(product_style)s
               AND garment_color_code = %(garment_color_code)s
               AND option_row = %(option_row)s
             ORDER BY position
            """,
            key,
        )
        rows = cursor.fetchall()
        assert len(rows) >= 2
        assert all(row["active"] is False for row in rows)
        assert all(row["updated_by"] == "kernel-test" for row in rows)


def test_hard_deleting_primary_removes_complete_option_row():
    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        key = _primary_with_companion(cursor)
        hard_delete_assignment(
            cursor,
            "kernel-test",
            HardDeleteAssignmentCommand(**key, position=1),
        )
        cursor.execute(
            """
            SELECT count(*) AS remaining FROM logo.assignment
             WHERE fdm4_store = %(fdm4_store)s
               AND product_style = %(product_style)s
               AND garment_color_code = %(garment_color_code)s
               AND option_row = %(option_row)s
            """,
            key,
        )
        assert cursor.fetchone()["remaining"] == 0


def test_color_soft_and_hard_operations_have_distinct_effects():
    for handler, command_type, hard in (
        (deactivate_color, DeactivateColorCommand, False),
        (hard_delete_color, HardDeleteColorCommand, True),
    ):
        with database.cursor(
            write=True,
            actor="kernel-test",
            commit_on_success=False,
        ) as cursor:
            row = _first_assignment(cursor)
            command = command_type(
                fdm4_store=row["fdm4_store"],
                product_style=row["product_style"],
                garment_color_code=row["garment_color_code"],
            )
            result = handler(cursor, "kernel-test", command)
            assert result.value["hard"] is hard
            cursor.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE active) AS active
                  FROM logo.assignment
                 WHERE fdm4_store = %s AND product_style = %s
                   AND garment_color_code = %s
                """,
                (
                    row["fdm4_store"],
                    row["product_style"],
                    row["garment_color_code"],
                ),
            )
            counts = cursor.fetchone()
            assert counts["total"] == (0 if hard else result.value["removed"])
            assert counts["active"] == 0


def test_style_and_apply_to_colors_return_style_scopes():
    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        row = _first_assignment(cursor)
        style_result = set_style_active(
            cursor,
            "kernel-test",
            SetStyleActiveCommand(
                store=row["fdm4_store"],
                style=row["product_style"],
                active=False,
            ),
        )
        assert style_result.value["updated"] >= 1
        assert style_result.scopes[0].kind == "assignment_style"

    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        row = _first_assignment(cursor)
        result = apply_to_colors(
            cursor,
            "kernel-test",
            ApplyToColorsCommand(
                store=row["fdm4_store"],
                style=row["product_style"],
                garment_color_code=row["garment_color_code"],
                position=row["position"],
                option_row=row["option_row"],
                overwrite=False,
            ),
        )
        assert result.value["colors"] >= 1
        assert result.scopes[0].kind == "assignment_style"


def test_copy_style_targets_only_the_destination_style_scope():
    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        source = _first_assignment(cursor)
        cursor.execute(
            """
            SELECT DISTINCT style_code
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND style_code <> %s AND is_active = true
             ORDER BY style_code LIMIT 1
            """,
            (source["fdm4_store"], source["product_style"]),
        )
        target = cursor.fetchone()
        assert target is not None, "Phase 0 seed must contain two styles"
        result = copy_style(
            cursor,
            "kernel-test",
            CopyStyleCommand(
                store=source["fdm4_store"],
                source_style=source["product_style"],
                target_style=target["style_code"],
                overwrite=False,
            ),
        )
        assert result.value["source_rows"] >= 1
        assert result.scopes[0].key["product_style"] == target["style_code"]


def test_settings_and_pricing_mutations_return_exact_row_scopes():
    with database.cursor(
        write=True,
        actor="kernel-test",
        commit_on_success=False,
    ) as cursor:
        assignment = _first_assignment(cursor)
        store = assignment["fdm4_store"]
        settings = update_store_settings(
            cursor,
            "kernel-test",
            UpdateStoreSettingsCommand(
                store=store,
                enabled=False,
                allows_none=True,
            ),
        )
        assert settings.value["settings"]["updated_by"] == "kernel-test"
        assert settings.scopes[0].kind == "store_settings_row"

        cursor.execute(
            "SELECT tier_name FROM woo.pricing_tier ORDER BY sort_order LIMIT 1"
        )
        tier = cursor.fetchone()["tier_name"]
        pricing = set_store_pricing_tier(
            cursor,
            "kernel-test",
            SetStorePricingTierCommand(
                fdm4_store=store,
                tier_name=tier,
                note="kernel contract",
            ),
        )
        assert pricing.value["assignment"]["tier_name"] == tier
        assert pricing.scopes[0].kind == "store_pricing_tier_row"
        deleted = delete_store_pricing_tier(
            cursor,
            "kernel-test",
            DeleteStorePricingTierCommand(fdm4_store=store),
        )
        assert deleted.value == {"ok": True}


def test_preview_rejects_handler_scope_drift_and_rolls_back_its_write(monkeypatch):
    with database.cursor() as cursor:
        row = _first_assignment(cursor)
    command = _save_command(row, location="SHOULD ROLL BACK")

    def wrong_scope_handler(cursor, actor, actual_command):
        del actor
        cursor.execute(
            """
            UPDATE logo.assignment SET location = %s
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s
               AND position = %s
            """,
            (
                actual_command.location,
                actual_command.fdm4_store,
                actual_command.product_style,
                actual_command.garment_color_code,
                actual_command.option_row,
                actual_command.position,
            ),
        )
        return MutationResult(
            {"ok": True},
            (MutationScope(
                "store_settings_row",
                {"fdm4_store": actual_command.fdm4_store},
            ),),
        )

    monkeypatch.setattr(staging, "dispatch_mutation", wrong_scope_handler)
    with pytest.raises(InvalidCommand, match="scope"):
        staging.preview_commands(
            [("save_assignment", command)],
            "admin-one",
        )
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT location FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s
               AND position = %s
            """,
            (
                row["fdm4_store"],
                row["product_style"],
                row["garment_color_code"],
                row["option_row"],
                row["position"],
            ),
        )
        assert cursor.fetchone()["location"] == row["location"]
