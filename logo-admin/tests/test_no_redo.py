"""Redo is intentionally absent from schema, registry, routes, and API."""

from db import database
from main import app
import staging
from tool_registry import TOOL_SPECS


def test_staging_has_no_redo_entrypoint():
    assert not hasattr(staging, "redo_change_set")


def test_no_route_or_model_tool_exposes_redo():
    assert all("redo" not in getattr(route, "path", "").lower() for route in app.routes)
    assert all("redo" not in spec.name.lower() for spec in TOOL_SPECS)


def test_schema_status_and_journal_events_exclude_redo():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_get_constraintdef(oid, true) AS definition
              FROM pg_constraint
             WHERE conrelid IN (
                 'logo.agent_change_set'::regclass,
                 'logo.agent_action_journal'::regclass
             ) AND contype='c'
            """
        )
        definitions = " ".join(row["definition"].lower() for row in cursor.fetchall())
    assert "redo" not in definitions
    assert "undone" in definitions
    assert "undo" in definitions
