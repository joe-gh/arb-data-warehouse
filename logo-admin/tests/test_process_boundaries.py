from pathlib import Path


def test_web_modules_never_use_dependency_overrides():
    offenders = []
    for path in Path(".").glob("*.py"):
        if path.name == "mcp_server.py":
            continue
        if "dependency_overrides" in path.read_text():
            offenders.append(path.name)
    assert offenders == []


def test_mcp_identity_is_unchanged():
    source = Path("mcp_server.py").read_text()
    assert (
        'return {"user_login": ACTOR, "display_name": f"{ACTOR} (MCP)", "csrf": "mcp"}'
        in source
    )
    assert "app_main.app.dependency_overrides[require_user]" in source
    assert "app_main.app.dependency_overrides[require_csrf]" in source
