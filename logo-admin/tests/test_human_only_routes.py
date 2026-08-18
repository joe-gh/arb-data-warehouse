"""Confirmation, apply, discard, and undo remain human-only HTTP actions."""

from authorization import HUMAN_ONLY_COMMANDS, require_agent_access
from main import app
from tool_registry import agent_tool_schemas


EXPECTED_LIFECYCLE = {
    ("GET", "/api/agent/change-sets/{change_set_id}"),
    ("POST", "/api/agent/change-sets/{change_set_id}/apply"),
    ("POST", "/api/agent/change-sets/{change_set_id}/discard"),
    ("POST", "/api/agent/change-sets/{change_set_id}/undo"),
    ("POST", "/api/agent/spreadsheets/{job_id}/confirm-mapping"),
}


def _route_map():
    result = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/agent/"):
            continue
        for method in getattr(route, "methods", set()):
            if method not in {"HEAD", "OPTIONS"}:
                result[(method, path)] = route
    return result


def test_every_human_lifecycle_route_exists_and_uses_single_access_dependency():
    routes = _route_map()
    assert EXPECTED_LIFECYCLE <= routes.keys()
    for key in EXPECTED_LIFECYCLE:
        dependency_calls = {
            dependency.call
            for dependency in routes[key].dependant.dependencies
        }
        assert require_agent_access in dependency_calls, key


def test_human_only_commands_never_appear_in_model_schema():
    names = {schema["name"] for schema in agent_tool_schemas(writes_enabled=True)}
    assert names.isdisjoint(HUMAN_ONLY_COMMANDS)


def test_unsafe_human_routes_are_post_not_get():
    assert all(
        method == "POST"
        for method, path in EXPECTED_LIFECYCLE
        if not path.endswith("{change_set_id}")
    )
