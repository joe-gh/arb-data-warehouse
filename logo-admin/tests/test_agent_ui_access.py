from pathlib import Path


APP = Path(__file__).resolve().parents[1]


def test_dashboard_context_uses_the_shared_access_predicate():
    source = (APP / "routes_pages.py").read_text()
    assert "from authorization import agent_access_allowed" in source
    assert '"agent_access_allowed": assistant_allowed' in source
    assert "assistant_allowed and settings.agent_writes_enabled" in source


def test_template_uses_server_side_conditions_not_css_authorization():
    source = (APP / "templates" / "dashboard.html").read_text()
    assert source.count("{% if agent_access_allowed %}") == 3
    assert "agent_async_guard.js" in source
    assert 'id="assistant-toggle"' in source
    assert 'id="assistant-fab"' in source
    assert 'id="assistant-panel"' in source
    assert "agent-unauthorized" not in source


def test_local_nginx_artifact_is_sse_safe():
    artifact = APP / "deploy" / "nginx" / "agent-chat.conf"
    source = artifact.read_text()
    assert "location = /api/agent/chat" in source
    assert "proxy_buffering off;" in source
    assert "proxy_cache off;" in source
    assert "gzip off;" in source
    assert "proxy_read_timeout 120s;" in source
    assert "proxy_send_timeout 120s;" in source


def test_service_artifacts_use_the_live_application_path():
    service = (APP / "logo-admin.service").read_text()
    maintenance = (APP / "agent-maintenance.service").read_text()
    stale_working_directory = "WorkingDirectory=/opt/arb-data-warehouse" + "/logo-admin"
    assert "WorkingDirectory=/opt/arb-logo-admin" in service
    assert "WorkingDirectory=/opt/arb-logo-admin" in maintenance
    assert stale_working_directory not in service
    assert stale_working_directory not in maintenance
    assert "/var/lib/arb-logo-admin/agent-uploads" in service


def test_operator_allowlist_is_documented_as_fail_closed():
    readme = (APP / "README.md").read_text()
    assert "AGENT_ALLOWED_USERS=" in readme
    assert "empty, which means nobody" in readme
    assert "Joseph's actual" in readme
