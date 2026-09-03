from dataclasses import replace

import pytest

from authorization import HUMAN_ONLY_COMMANDS
from read_commands import ListStoresCommand
from tool_registry import (
    APPROVED_AGENT_READ_NAMES,
    APPROVED_AGENT_WRITE_NAMES,
    EXCLUDED_AGENT_TOOLS,
    TOOL_SPECS,
    ToolRegistryError,
    agent_tool_schemas,
    validate_registry,
)


def test_registry_contains_bounded_reads_and_exact_approved_writes():
    reads = [spec for spec in TOOL_SPECS if spec.kind == "read"]
    writes = [spec for spec in TOOL_SPECS if spec.kind == "write"]
    assert len(reads) == 20
    assert {spec.name for spec in reads} == APPROVED_AGENT_READ_NAMES
    assert {spec.name for spec in writes} == APPROVED_AGENT_WRITE_NAMES
    assert all(spec.agent_enabled and spec.bounded for spec in TOOL_SPECS)
    names = {spec.name for spec in TOOL_SPECS}
    assert names.isdisjoint(HUMAN_ONLY_COMMANDS)
    assert names.isdisjoint(EXCLUDED_AGENT_TOOLS)


def test_openai_schemas_are_strict_and_closed():
    for schema in agent_tool_schemas():
        assert schema["strict"] is True
        parameters = schema["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters.get("properties", {}))


def test_duplicate_tool_fails():
    with pytest.raises(ToolRegistryError, match="duplicate"):
        validate_registry((TOOL_SPECS[0], TOOL_SPECS[0]))


def test_unbounded_agent_tool_fails():
    unsafe = replace(TOOL_SPECS[0], bounded=False)
    with pytest.raises(ToolRegistryError, match="unsafe"):
        validate_registry((unsafe,))


def test_excluded_agent_tool_fails():
    unsafe = replace(TOOL_SPECS[0], name=next(iter(EXCLUDED_AGENT_TOOLS)))
    with pytest.raises(ToolRegistryError, match="excluded"):
        validate_registry((unsafe,))


def test_writes_enabled_fails_when_an_approved_write_is_missing():
    missing = next(iter(APPROVED_AGENT_WRITE_NAMES))
    incomplete = tuple(spec for spec in TOOL_SPECS if spec.name != missing)
    with pytest.raises(ToolRegistryError, match="registry is incomplete") as exc:
        validate_registry(incomplete, writes_enabled=True)
    assert missing in str(exc.value)


def test_read_registry_must_remain_exact_even_when_writes_are_disabled():
    invented = replace(TOOL_SPECS[0], name="bounded_but_unapproved_read")
    specs = (invented,) + TOOL_SPECS[1:]
    with pytest.raises(ToolRegistryError, match="unapproved agent read") as exc:
        validate_registry(specs, writes_enabled=False)
    assert "bounded_but_unapproved_read" in str(exc.value)


def test_read_handler_identity_is_pinned():
    replaced = replace(TOOL_SPECS[0], handler=lambda *_args: {})
    with pytest.raises(ToolRegistryError, match="read handler is not canonical"):
        validate_registry((replaced,) + TOOL_SPECS[1:])


def test_read_model_identity_is_pinned():
    replaced = replace(TOOL_SPECS[0], command_model=ListStoresCommand)
    other = TOOL_SPECS[1]
    replaced_other = replace(other, command_model=ListStoresCommand)
    # The first replacement is intentionally a no-op; the second proves an
    # equally strict but wrong read model cannot be swapped in by name.
    assert replaced.command_model is TOOL_SPECS[0].command_model
    with pytest.raises(ToolRegistryError, match="read command model is not canonical"):
        validate_registry((TOOL_SPECS[0], replaced_other) + TOOL_SPECS[2:])
