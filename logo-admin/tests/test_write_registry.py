"""Startup registry validation fails closed for every unsafe write shape."""

from dataclasses import replace

import pytest

from authorization import HUMAN_ONLY_COMMANDS
from commands import COMMAND_MODELS
import mutations
import snapshots
from tool_registry import (
    CANONICAL_AGENT_WRITE_CONTRACTS,
    EXCLUDED_AGENT_TOOLS,
    TOOL_SPECS,
    agent_tool_schemas,
    validate_registry,
)


EXPECTED_WRITES = set(COMMAND_MODELS)
EXPECTED_SCOPES = {
    "save_assignment": frozenset({"assignment_option_row"}),
    "deactivate_assignment": frozenset({"assignment_option_row"}),
    "hard_delete_assignment": frozenset({"assignment_option_row"}),
    "deactivate_color": frozenset({"assignment_color"}),
    "hard_delete_color": frozenset({"assignment_color"}),
    "set_style_active": frozenset({"assignment_style"}),
    "apply_to_colors": frozenset({"assignment_style"}),
    "copy_style": frozenset({"assignment_style"}),
    "update_store_settings": frozenset({"store_settings_row"}),
    "set_store_pricing_tier": frozenset({"store_pricing_tier_row"}),
    "delete_store_pricing_tier": frozenset({"store_pricing_tier_row"}),
    "copy_style_to_many": frozenset({"assignment_style"}),
    "paste_logo_set": frozenset({"assignment_style"}),
    "replace_design": frozenset({"assignment_style"}),
    "reorder_logo_rows": frozenset({"assignment_style"}),
    "set_styles_active": frozenset({"assignment_style"}),
}


def _write_specs():
    return {spec.name: spec for spec in TOOL_SPECS if spec.kind == "write"}


def test_registry_contains_exactly_approved_typed_agent_writes():
    writes = _write_specs()
    assert set(writes) == EXPECTED_WRITES
    assert set(CANONICAL_AGENT_WRITE_CONTRACTS) == EXPECTED_WRITES
    for name, spec in writes.items():
        contract = CANONICAL_AGENT_WRITE_CONTRACTS[name]
        assert spec.command_model is contract.command_model
        assert spec.handler is contract.handler
        assert contract.scope_kinds == EXPECTED_SCOPES[name]
        assert contract.snapshot_scope_kinds == EXPECTED_SCOPES[name]
        assert contract.restore_scope_kinds == EXPECTED_SCOPES[name]
        assert spec.agent_enabled is True
        assert spec.bounded is True
        assert spec.transactional is True
        assert spec.undoable is True
        assert callable(spec.handler)
        assert COMMAND_MODELS[name] is contract.command_model
        assert mutations.MUTATION_HANDLERS[name] is contract.handler
        assert mutations.COMMAND_SCOPE_KINDS[name] == EXPECTED_SCOPES[name]
        assert EXPECTED_SCOPES[name] <= snapshots.SNAPSHOT_SCOPE_KINDS
        assert EXPECTED_SCOPES[name] <= snapshots.RESTORE_SCOPE_KINDS


def test_canonical_write_contract_map_is_immutable():
    with pytest.raises(TypeError):
        CANONICAL_AGENT_WRITE_CONTRACTS["invented"] = (  # type: ignore[index]
            CANONICAL_AGENT_WRITE_CONTRACTS["save_assignment"]
        )


@pytest.mark.parametrize(
    "unsafe_change",
    [
        {"handler": None},
        {"transactional": False},
        {"undoable": False},
        {"bounded": False},
    ],
)
def test_writes_enabled_startup_rejects_unsafe_specs(unsafe_change):
    safe = next(iter(_write_specs().values()))
    unsafe = replace(safe, **unsafe_change)
    specs = tuple(spec for spec in TOOL_SPECS if spec.name != safe.name) + (unsafe,)
    with pytest.raises(RuntimeError):
        validate_registry(specs, writes_enabled=True)


def test_model_schema_never_contains_human_lifecycle_or_excluded_tools():
    names = {schema["name"] for schema in agent_tool_schemas(writes_enabled=True)}
    assert names.isdisjoint(HUMAN_ONLY_COMMANDS)
    assert names.isdisjoint(EXCLUDED_AGENT_TOOLS)
    assert EXPECTED_WRITES <= names


def test_read_only_mode_excludes_all_write_schemas():
    names = {schema["name"] for schema in agent_tool_schemas(writes_enabled=False)}
    assert names.isdisjoint(EXPECTED_WRITES)


def test_startup_rejects_an_unknown_write_scope(monkeypatch):
    monkeypatch.setitem(
        mutations.COMMAND_SCOPE_KINDS,
        "save_assignment",
        frozenset({"scope_without_snapshot_support"}),
    )
    with pytest.raises(RuntimeError, match="scope mapping is not canonical"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_supported_but_wrong_write_scope(monkeypatch):
    # assignment_color is fully snapshot/restorable, but save_assignment is
    # allowed to report only assignment_option_row.
    monkeypatch.setitem(
        mutations.COMMAND_SCOPE_KINDS,
        "save_assignment",
        frozenset({"assignment_color"}),
    )
    with pytest.raises(RuntimeError, match="scope mapping is not canonical"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_missing_restorer_declaration(monkeypatch):
    monkeypatch.setattr(
        snapshots,
        "RESTORE_SCOPE_KINDS",
        snapshots.RESTORE_SCOPE_KINDS - {"assignment_option_row"},
    )
    with pytest.raises(RuntimeError, match="snapshot/restore scope"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_missing_snapshot_declaration(monkeypatch):
    monkeypatch.setattr(
        snapshots,
        "SNAPSHOT_SCOPE_KINDS",
        snapshots.SNAPSHOT_SCOPE_KINDS - {"assignment_option_row"},
    )
    with pytest.raises(RuntimeError, match="snapshot/restore scope"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_extra_snapshot_scope_declaration(monkeypatch):
    monkeypatch.setattr(
        snapshots,
        "SNAPSHOT_SCOPE_KINDS",
        snapshots.SNAPSHOT_SCOPE_KINDS | {"invented_scope"},
    )
    with pytest.raises(RuntimeError, match="canonical set"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


@pytest.mark.parametrize(
    ("registry_name", "registry"),
    [
        ("models", COMMAND_MODELS),
        ("handlers", mutations.MUTATION_HANDLERS),
        ("scope mappings", mutations.COMMAND_SCOPE_KINDS),
    ],
)
def test_startup_rejects_missing_mutable_registry_entry(
    monkeypatch,
    registry_name,
    registry,
):
    monkeypatch.delitem(registry, "save_assignment")
    with pytest.raises(RuntimeError, match=f"write {registry_name} are not exact"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


@pytest.mark.parametrize(
    ("registry_name", "registry", "value"),
    [
        ("models", COMMAND_MODELS, COMMAND_MODELS["save_assignment"]),
        (
            "handlers",
            mutations.MUTATION_HANDLERS,
            mutations.MUTATION_HANDLERS["save_assignment"],
        ),
        (
            "scope mappings",
            mutations.COMMAND_SCOPE_KINDS,
            frozenset({"assignment_option_row"}),
        ),
    ],
)
def test_startup_rejects_extra_mutable_registry_entry(
    monkeypatch,
    registry_name,
    registry,
    value,
):
    monkeypatch.setitem(registry, "invented_write", value)
    with pytest.raises(RuntimeError, match=f"write {registry_name} are not exact"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_coordinated_mutable_registry_rewiring(monkeypatch):
    writes = _write_specs()
    source = writes["save_assignment"]
    wrong = writes["deactivate_color"]
    monkeypatch.setitem(COMMAND_MODELS, source.name, wrong.command_model)
    monkeypatch.setitem(mutations.MUTATION_HANDLERS, source.name, wrong.handler)
    monkeypatch.setitem(
        mutations.COMMAND_SCOPE_KINDS,
        source.name,
        mutations.COMMAND_SCOPE_KINDS[wrong.name],
    )
    coordinated = replace(
        source,
        command_model=wrong.command_model,
        handler=wrong.handler,
    )
    specs = tuple(
        coordinated if spec.name == source.name else spec
        for spec in TOOL_SPECS
    )

    with pytest.raises(RuntimeError, match="command model is not canonical"):
        validate_registry(specs, writes_enabled=True)


def test_startup_rejects_swapped_mutable_handlers_even_with_canonical_specs(
    monkeypatch,
):
    first = mutations.MUTATION_HANDLERS["save_assignment"]
    second = mutations.MUTATION_HANDLERS["deactivate_color"]
    monkeypatch.setitem(mutations.MUTATION_HANDLERS, "save_assignment", second)
    monkeypatch.setitem(mutations.MUTATION_HANDLERS, "deactivate_color", first)

    with pytest.raises(RuntimeError, match="handler is not canonical"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_swapped_mutable_models_even_with_canonical_specs(
    monkeypatch,
):
    first = COMMAND_MODELS["save_assignment"]
    second = COMMAND_MODELS["deactivate_color"]
    monkeypatch.setitem(COMMAND_MODELS, "save_assignment", second)
    monkeypatch.setitem(COMMAND_MODELS, "deactivate_color", first)

    with pytest.raises(RuntimeError, match="command model is not canonical"):
        validate_registry(TOOL_SPECS, writes_enabled=True)


def test_startup_rejects_swapped_write_spec_model():
    writes = _write_specs()
    source = writes["save_assignment"]
    wrong = writes["deactivate_color"]
    swapped = replace(source, command_model=wrong.command_model)
    specs = tuple(
        swapped if spec.name == source.name else spec
        for spec in TOOL_SPECS
    )

    with pytest.raises(RuntimeError, match="command model is not canonical"):
        validate_registry(specs, writes_enabled=True)


def test_startup_rejects_swapped_write_spec_handler():
    writes = _write_specs()
    source = writes["save_assignment"]
    wrong = writes["deactivate_color"]
    swapped = replace(source, handler=wrong.handler)
    specs = tuple(
        swapped if spec.name == source.name else spec
        for spec in TOOL_SPECS
    )

    with pytest.raises(RuntimeError, match="handler is not canonical"):
        validate_registry(specs, writes_enabled=True)
