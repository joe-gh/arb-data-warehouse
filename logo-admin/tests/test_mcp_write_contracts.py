"""The privileged MCP adapter keeps its established HTTP transport shapes."""

from types import SimpleNamespace

import pytest

import mcp_server


def _recorder(monkeypatch):
    calls = []

    def fake_call(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_call", fake_call)
    return calls


def test_mcp_assignment_soft_hard_and_save_shapes(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.save_assignment(
        "S_TEST", "STYLE", "BLK", 1, "D1", "L1", "SC1"
    )
    mcp_server.delete_assignment("S_TEST", "STYLE", "BLK", 1, hard=False)
    mcp_server.delete_assignment("S_TEST", "STYLE", "BLK", 1, hard=True)
    assert calls[0][0:2] == ("PUT", "/api/assignments")
    assert calls[0][2]["json_body"]["fdm4_store"] == "S_TEST"
    assert calls[1][0:2] == ("DELETE", "/api/assignments")
    assert calls[1][2]["params"]["hard"] is False
    assert calls[2][2]["params"]["hard"] is True


def test_mcp_assignment_override_is_three_state(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.save_assignment(
        "S_TEST", "STYLE", "BLK", 1, "D1", "L1", "SC1"
    )
    mcp_server.save_assignment(
        "S_TEST", "STYLE", "BLK", 1, "D1", "L1", "SC1",
        name_override="",
        expected_updated_at="2026-08-01T12:00:00+00:00",
    )
    assert "name_override" not in calls[0][2]["json_body"]
    assert calls[1][2]["json_body"]["name_override"] == ""
    assert calls[1][2]["json_body"]["expected_updated_at"].startswith("2026-")


def test_mcp_bulk_transactional_writes_keep_paths(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.clear_color("S_TEST", "STYLE", "BLK", hard=True)
    mcp_server.set_style_active("S_TEST", "STYLE", False)
    mcp_server.apply_to_all_colors("S_TEST", "STYLE", "BLK", 1)
    mcp_server.copy_style("S_TEST", "SOURCE", "TARGET", overwrite=True)
    mcp_server.update_store_settings("S_TEST", False, True)
    assert [(method, path) for method, path, _ in calls] == [
        ("DELETE", "/api/assignments-by-color"),
        ("POST", "/api/style-active"),
        ("POST", "/api/apply-all-colors"),
        ("POST", "/api/copy-style"),
        ("PUT", "/api/settings/S_TEST"),
    ]


def test_mcp_pricing_writes_keep_paths(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.set_store_tier("S_TEST", "MSRP", "note")
    mcp_server.delete_store_tier("S_TEST")
    assert calls == [
        (
            "PUT",
            "/api/pricing/store-tier",
            {
                "json_body": {
                    "fdm4_store": "S_TEST",
                    "tier_name": "MSRP",
                    "note": "note",
                }
            },
        ),
        (
            "DELETE",
            "/api/pricing/store-tier",
            {"params": {"fdm4_store": "S_TEST"}},
        ),
    ]


def test_mcp_nontransactional_tools_remain_available_only_through_adapter(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.sync_to_wordpress("S_TEST", ["STYLE"])
    mcp_server.import_assignments_csv("header\nvalue", "S_TEST")
    mcp_server.mirror_legacy_images(3)
    assert [path for _method, path, _kwargs in calls] == [
        "/api/sync",
        "/api/import",
        "/api/legacy-import-images",
    ]


def test_mcp_rejects_oversized_base64_before_decode(monkeypatch):
    monkeypatch.setattr(
        mcp_server, "get_settings", lambda: SimpleNamespace(max_upload_bytes=3)
    )
    with pytest.raises(RuntimeError, match="exceeds"):
        mcp_server.upload_image("QUJDRA==", "image.png")
    with pytest.raises(RuntimeError, match="valid base64"):
        mcp_server.upload_image("!!!!", "image.png")


def test_mcp_legacy_import_is_confined_to_import_root(monkeypatch, tmp_path):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    allowed = import_root / "input.ndjson"
    allowed.write_text('{"ok": true}\n', encoding="utf-8")
    outside = tmp_path / "outside.ndjson"
    outside.write_text('{"ok": false}\n', encoding="utf-8")
    monkeypatch.setenv("MCP_IMPORT_DIR", str(import_root))
    calls = _recorder(monkeypatch)
    mcp_server.import_legacy_ndjson("input.ndjson")
    assert calls[-1][1] == "/api/legacy-import"
    with pytest.raises(RuntimeError, match="inside MCP_IMPORT_DIR"):
        mcp_server.import_legacy_ndjson(str(outside))
    linked = import_root / "linked.ndjson"
    linked.symlink_to(outside)
    with pytest.raises(RuntimeError, match="symbolic link"):
        mcp_server.import_legacy_ndjson(str(linked))
