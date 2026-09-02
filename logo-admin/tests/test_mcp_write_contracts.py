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


def test_mcp_category_tools_keep_paths(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.cat_targets()
    mcp_server.cat_snapshot_status("dev")
    mcp_server.cat_list_blogs("dev")
    mcp_server.cat_wp_status("prod")
    mcp_server.cat_snapshot_import("dev", [1, 2, 59])
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/categories/targets"),
        ("GET", "/api/categories/snapshots"),
        ("GET", "/api/categories/blogs"),
        ("GET", "/api/categories/wp-status"),
        ("POST", "/api/categories/snapshots/import"),
    ]
    assert calls[1][2]["params"] == {"env": "dev"}
    assert calls[4][2]["json_body"] == {"env": "dev", "blog_ids": [1, 2, 59]}


def test_mcp_category_draft_tools_keep_paths(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.cat_tree_get()
    mcp_server.cat_tree_get(blog_id=61)
    mcp_server.cat_node_create("Clothing", parent_id=None, slug="clothing")
    mcp_server.cat_node_update(5, name="PPE")
    mcp_server.cat_node_move(5, 2, position=0)
    mcp_server.cat_node_delete(5, cascade=True)
    mcp_server.cat_draft_seed("prod", 1, force=False)
    mcp_server.cat_overrides_list(blog_id=61)
    mcp_server.cat_override_set(61, "rename", node_id=5, name="Stickers & Patches")
    mcp_server.cat_override_delete(9)
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/categories/tree"),
        ("GET", "/api/categories/tree/effective"),
        ("POST", "/api/categories/nodes"),
        ("PUT", "/api/categories/nodes/5"),
        ("POST", "/api/categories/nodes/5/move"),
        ("DELETE", "/api/categories/nodes/5"),
        ("POST", "/api/categories/draft/seed"),
        ("GET", "/api/categories/overrides"),
        ("PUT", "/api/categories/overrides"),
        ("DELETE", "/api/categories/overrides/9"),
    ]
    assert calls[1][2]["params"] == {"blog_id": 61}
    assert calls[2][2]["json_body"]["slug"] == "clothing"
    assert calls[6][2]["json_body"] == {"env": "prod", "blog_id": 1, "force": False}
    assert calls[8][2]["json_body"]["kind"] == "rename"


def test_mcp_category_mapping_tools_keep_paths(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.cat_mapping_status("prod")
    mcp_server.cat_mapping_suggest("prod")
    mcp_server.cat_mapping_set("men-s", "map", target_node_id=3)
    mcp_server.cat_mapping_bulk([{"old_slug": "saws", "action": "delete"}])
    mcp_server.cat_mapping_clear("men-s")
    mcp_server.cat_rules_list(4)
    mcp_server.cat_rule_evaluate("prod", {"field": "name", "op": "prefix", "value": "Women"})
    mcp_server.cat_rule_set(4, {"field": "brand", "op": "equals", "value": "Arborwear"})
    mcp_server.cat_rule_delete(8)
    mcp_server.cat_assignments_list(4)
    mcp_server.cat_assign(4, ["A1"], mode="remove")
    mcp_server.cat_membership("prod", 4)
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/categories/mapping"),
        ("GET", "/api/categories/mapping/suggest"),
        ("PUT", "/api/categories/mapping"),
        ("PUT", "/api/categories/mapping"),
        ("DELETE", "/api/categories/mapping/men-s"),
        ("GET", "/api/categories/rules"),
        ("POST", "/api/categories/rules/evaluate"),
        ("PUT", "/api/categories/rules"),
        ("DELETE", "/api/categories/rules/8"),
        ("GET", "/api/categories/assignments"),
        ("PUT", "/api/categories/assignments"),
        ("GET", "/api/categories/membership"),
    ]
    assert calls[2][2]["json_body"]["rows"][0]["target_node_id"] == 3
    assert calls[10][2]["json_body"]["mode"] == "remove"


def test_mcp_category_run_tools_keep_paths(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.cat_preview("prod")
    mcp_server.cat_preview_blog("prod", 1)
    mcp_server.cat_ack_set(["A1"], note="n")
    mcp_server.cat_runs("prod")
    mcp_server.cat_run_create("prod", blog_ids=[1], start=False)
    mcp_server.cat_run_status(3)
    mcp_server.cat_run_start(3)
    mcp_server.cat_run_pause(3)
    mcp_server.cat_run_resume(3)
    mcp_server.cat_run_cancel(3)
    mcp_server.cat_job_retry(3, 9)
    mcp_server.cat_job_skip(3, 9)
    mcp_server.cat_restore_blog(3, 9)
    mcp_server.cat_freeze_set("dev", True)
    mcp_server.cat_drift_audit("dev")
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/api/categories/preview"),
        ("GET", "/api/categories/preview/blog"),
        ("PUT", "/api/categories/uncategorized-ack"),
        ("GET", "/api/categories/runs"),
        ("POST", "/api/categories/runs"),
        ("GET", "/api/categories/runs/3"),
        ("POST", "/api/categories/runs/3/start"),
        ("POST", "/api/categories/runs/3/pause"),
        ("POST", "/api/categories/runs/3/resume"),
        ("POST", "/api/categories/runs/3/cancel"),
        ("POST", "/api/categories/runs/3/jobs/9/retry"),
        ("POST", "/api/categories/runs/3/jobs/9/skip"),
        ("POST", "/api/categories/runs/3/jobs/9/restore"),
        ("POST", "/api/categories/freeze"),
        ("POST", "/api/categories/drift-audit"),
    ]
    assert calls[4][2]["json_body"]["start"] is False
    assert calls[13][2]["json_body"] == {"env": "dev", "on": True}
