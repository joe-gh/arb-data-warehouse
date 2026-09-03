"""AGENT_WRITE_TOOLS: stage one mutation tool at a time during the pilot."""
import pytest

import tool_registry
from tool_registry import (
    APPROVED_AGENT_READ_NAMES,
    APPROVED_AGENT_WRITE_NAMES,
    ToolRegistryError,
    UnknownTool,
    agent_tool_schemas,
    get_agent_tool,
    validate_write_tool_allowlist,
)


def _names(schemas):
    return {schema["name"] for schema in schemas}


def test_empty_allowlist_advertises_every_approved_write():
    names = _names(agent_tool_schemas(writes_enabled=True, write_tools=frozenset()))
    assert names == APPROVED_AGENT_READ_NAMES | APPROVED_AGENT_WRITE_NAMES


def test_allowlist_narrows_advertised_writes_but_never_reads():
    names = _names(agent_tool_schemas(writes_enabled=True, write_tools={"save_assignment"}))
    assert names == APPROVED_AGENT_READ_NAMES | {"save_assignment"}
    # Reads are unaffected in read-only mode too.
    assert _names(agent_tool_schemas(writes_enabled=False, write_tools={"save_assignment"})) == APPROVED_AGENT_READ_NAMES


def test_dispatch_refuses_a_write_outside_the_allowlist():
    assert get_agent_tool("save_assignment", writes_enabled=True, write_tools={"save_assignment"}).name == "save_assignment"
    with pytest.raises(UnknownTool):
        get_agent_tool("copy_style", writes_enabled=True, write_tools={"save_assignment"})
    # Reads keep working regardless of the allowlist.
    assert get_agent_tool("get_style", writes_enabled=True, write_tools={"save_assignment"}).kind == "read"


def test_unknown_names_fail_closed():
    with pytest.raises(ToolRegistryError, match="not approved"):
        validate_write_tool_allowlist({"save_assignment", "drop_database"})
    with pytest.raises(ToolRegistryError):
        agent_tool_schemas(writes_enabled=True, write_tools={"nope"})


def test_allowlist_is_case_insensitive_and_trimmed():
    assert validate_write_tool_allowlist({" Save_Assignment "}) == frozenset({"save_assignment"})


def test_advertised_schemas_are_accepted_by_openai_strict_mode():
    """OpenAI rejects regex lookaround (seen live 2026-09-03 on save_assignment.cost_override)."""
    import json, re
    lookaround = re.compile(r"\(\?[=!<]")
    for schema in agent_tool_schemas(writes_enabled=True, write_tools=frozenset()):
        text = json.dumps(schema["parameters"])
        assert not lookaround.search(text), schema["name"]
        for prop, spec in schema["parameters"]["properties"].items():
            for branch in spec.get("anyOf", [spec]):
                assert "pattern" not in branch or not lookaround.search(branch["pattern"]), f"{schema['name']}.{prop}"
