from pathlib import Path

from authorization import (
    AccessContext,
    HUMAN_ONLY_COMMANDS,
    agent_access_allowed,
    assert_agent_callable,
    provider_safety_identifier,
    required_tier,
)
from config import get_settings


def test_all_current_commands_resolve_to_admin():
    assert required_tier("list_stores") == "admin"
    assert required_tier("hard_delete_assignment") == "admin"


def test_human_only_commands_are_not_agent_callable():
    for command in HUMAN_ONLY_COMMANDS:
        try:
            assert_agent_callable(command)
        except ValueError:
            pass
        else:
            raise AssertionError(command)


def test_allowlist_is_case_insensitive_and_fail_closed(agent_enabled):
    assert agent_access_allowed({"user_login": "Admin-One"}, agent_enabled)
    assert not agent_access_allowed({"user_login": "admin-two"}, agent_enabled)
    disabled = agent_enabled.__class__(
        **{**agent_enabled.__dict__, "agent_enabled": False}
    )
    assert not agent_access_allowed({"user_login": "admin-one"}, disabled)


def test_owner_identity_is_normalized_and_provider_identifier_is_pseudonymous(
    agent_enabled,
):
    context = AccessContext.from_session({
        "user_login": "  Admin-One ",
        "display_name": "Admin",
    })
    assert context.user_login == "admin-one"
    first = provider_safety_identifier("Admin-One", agent_enabled)
    assert first == provider_safety_identifier(" admin-one ", agent_enabled)
    assert first != provider_safety_identifier("admin-two", agent_enabled)
    assert "admin-one" not in first
    assert len(first) == 64


def test_session_has_no_tier():
    assert '"tier"' not in Path("auth.py").read_text()


def test_wordpress_broker_keeps_manage_options_gate():
    broker = Path(
        "/Users/josephdigiovanna/Projects/arborwear/WP2/"
        "wp-content/plugins/arb-admin/arb-logo-admin-api.php"
    ).read_text()
    assert "manage_options" in broker
    assert "arb_logo_admin_capability" in broker
