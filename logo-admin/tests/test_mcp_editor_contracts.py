"""MCP editor tools are thin route adapters (no DB)."""

import mcp_server


def _recorder(monkeypatch):
    calls = []

    def fake_call(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_call", fake_call)
    return calls


def test_mcp_has_editor_tools():
    assert {
        "reorder_option_rows", "set_style_color_order", "paste_assignments",
        "paste_assignments_batch", "set_styles_active",
        "copy_style_batch_preview", "copy_style_batch",
        "design_swap_preview", "design_swap",
        "find_similar_styles", "store_logo_coverage",
    } <= set(mcp_server.tool_names())


def test_reorder_tool_shape(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.reorder_option_rows("S_TEST", "STYLE-1", "RED", [2, 1], "style")
    assert calls[0][0:2] == ("POST", "/api/assignments/reorder")
    assert calls[0][2]["json_body"] == {
        "store": "S_TEST", "style": "STYLE-1", "garment_color_code": "RED",
        "option_rows": [2, 1], "apply_to": "style",
    }


def test_style_color_order_tool_shape(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.set_style_color_order("STYLE-1", ["BLU", "RED"])
    assert calls[0][0:2] == ("PUT", "/api/style-color-order")
    assert calls[0][2]["json_body"] == {"style": "STYLE-1", "colors": ["BLU", "RED"]}


def test_paste_tool_shape(monkeypatch):
    calls = _recorder(monkeypatch)
    rows = [{"option_row": 1, "position": 1, "design_id": "DESIGN-1", "logo_code": "C1",
             "color_scheme_id": "SCHEME-1", "location": "Left Chest", "optional": False,
             "background": "", "cost_override": None, "sort_order": 0, "image_url": "",
             "name_override": "Pasted name", "active": True}]
    mcp_server.paste_assignments("S_TEST", "STYLE-1", ["BLU", "GRN"], rows,
                                 overwrite=True, as_new_rows=True)
    assert calls[0][0:2] == ("POST", "/api/assignments/paste")
    assert calls[0][2]["json_body"] == {
        "store": "S_TEST", "style": "STYLE-1", "colors": ["BLU", "GRN"],
        "rows": rows, "overwrite": True, "as_new_rows": True,
    }
    assert calls[0][2]["json_body"]["rows"] is rows  # passes through untouched


def test_batch_tool_shapes(monkeypatch):
    calls = _recorder(monkeypatch)
    rows = [{"option_row": 1, "position": 1, "design_id": "DESIGN-1"}]
    mcp_server.paste_assignments_batch("S_TEST", ["STYLE-1", "STYLE-2"], rows)
    mcp_server.paste_assignments_batch("S_TEST", ["STYLE-1"], rows, color_scope="match",
                                       match_color="BLU", overwrite=True, as_new_rows=True)
    mcp_server.set_styles_active("S_TEST", ["STYLE-1", "STYLE-2"], False)
    assert calls[0][0:2] == ("POST", "/api/assignments/paste-batch")
    assert calls[0][2]["json_body"] == {
        "store": "S_TEST", "styles": ["STYLE-1", "STYLE-2"], "rows": rows,
        "color_scope": "match", "overwrite": False, "as_new_rows": False,
    }
    assert "match_color" not in calls[0][2]["json_body"]  # omitted when None
    assert calls[1][2]["json_body"]["match_color"] == "BLU"
    assert calls[1][2]["json_body"]["overwrite"] is True
    assert calls[1][2]["json_body"]["as_new_rows"] is True
    assert calls[2][0:2] == ("POST", "/api/style-active-batch")
    assert calls[2][2]["json_body"] == {
        "store": "S_TEST", "styles": ["STYLE-1", "STYLE-2"], "active": False,
    }


def test_copy_style_batch_tool_shapes(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.copy_style_batch_preview("S_TEST", "STYLE-1", ["STYLE-2", "STYLE-3"])
    mcp_server.copy_style_batch("S_TEST", "STYLE-1", ["STYLE-2"], color_match="like", mode="replace")
    assert calls[0][0:2] == ("POST", "/api/copy-style-batch/preview")
    assert calls[0][2]["json_body"] == {
        "store": "S_TEST", "source_style": "STYLE-1",
        "target_styles": ["STYLE-2", "STYLE-3"], "color_match": "exact",
    }
    assert calls[1][0:2] == ("POST", "/api/copy-style-batch")
    assert calls[1][2]["json_body"] == {
        "store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
        "color_match": "like", "mode": "replace",
    }


def test_design_swap_tool_shapes(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.design_swap_preview("S_TEST", "DESIGN-1", "DESIGN-2", "SCHEME-2")
    mcp_server.design_swap("S_TEST", "DESIGN-1", "DESIGN-2", "SCHEME-2",
                           from_color_scheme_id="SCHEME-1", to_logo_code="C2", styles=["STYLE-1"])
    assert calls[0][0:2] == ("POST", "/api/design-swap/preview")
    assert calls[0][2]["json_body"] == {
        "store": "S_TEST", "from_design_id": "DESIGN-1",
        "to_design_id": "DESIGN-2", "to_color_scheme_id": "SCHEME-2",
    }
    assert calls[1][0:2] == ("POST", "/api/design-swap")
    assert calls[1][2]["json_body"] == {
        "store": "S_TEST", "from_design_id": "DESIGN-1",
        "to_design_id": "DESIGN-2", "to_color_scheme_id": "SCHEME-2",
        "from_color_scheme_id": "SCHEME-1", "to_logo_code": "C2", "styles": ["STYLE-1"],
    }


def test_discovery_tool_shapes(monkeypatch):
    calls = _recorder(monkeypatch)
    mcp_server.find_similar_styles("S_TEST", "STYLE-1")
    mcp_server.find_similar_styles("S_TEST", "STYLE-1", "overlap")
    mcp_server.store_logo_coverage("S_TEST")
    mcp_server.store_logo_coverage("S_TEST", unconfigured_only=False)
    assert calls[0][0:2] == ("GET", "/api/styles/similar")
    assert calls[0][2]["params"] == {"store": "S_TEST", "style": "STYLE-1", "mode": "exact"}
    assert calls[1][2]["params"]["mode"] == "overlap"
    assert calls[2][0:2] == ("GET", "/api/styles/coverage")
    assert calls[2][2]["params"] == {"store": "S_TEST", "unconfigured_only": True}
    assert calls[3][2]["params"]["unconfigured_only"] is False
