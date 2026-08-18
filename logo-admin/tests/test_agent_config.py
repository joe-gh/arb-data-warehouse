import pytest

from config import ConfigurationError, get_settings


def _reload():
    get_settings.cache_clear()
    return get_settings()


def test_disabled_agent_does_not_require_openai(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    settings = _reload()
    assert settings.agent_enabled is False
    assert settings.openai_api_key is None


def test_enabled_agent_requires_key(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        _reload()


def test_enabled_agent_requires_model(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ConfigurationError, match="OPENAI_MODEL"):
        _reload()


def test_writes_require_agent(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    monkeypatch.setenv("AGENT_WRITES_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="requires AGENT_ENABLED"):
        _reload()


def test_allowlist_is_lowercased_trimmed_and_empty_is_valid(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    monkeypatch.setenv("AGENT_WRITES_ENABLED", "false")
    monkeypatch.setenv("AGENT_ALLOWED_USERS", " Joseph, ADMIN-ONE, joseph, ")
    assert _reload().agent_allowed_users == frozenset({"joseph", "admin-one"})
    monkeypatch.setenv("AGENT_ALLOWED_USERS", "")
    assert _reload().agent_allowed_users == frozenset()


def test_repull_function_hash_is_optional_normalized_and_validated(monkeypatch):
    monkeypatch.delenv("AGENT_REPULL_FUNCTION_SHA256", raising=False)
    assert _reload().agent_repull_function_sha256 is None
    monkeypatch.setenv("AGENT_REPULL_FUNCTION_SHA256", "A" * 64)
    assert _reload().agent_repull_function_sha256 == "a" * 64
    monkeypatch.setenv("AGENT_REPULL_FUNCTION_SHA256", "not-a-hash")
    with pytest.raises(ConfigurationError, match="AGENT_REPULL_FUNCTION_SHA256"):
        _reload()


@pytest.mark.parametrize("name,value", [
    ("AGENT_REQUESTS_PER_MINUTE", "zero"),
    ("AGENT_MAX_TOOL_CALLS", "0"),
    ("AGENT_TURN_TIMEOUT_SECONDS", "9999"),
])
def test_agent_caps_fail_closed(monkeypatch, name, value):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        _reload()


def test_tool_result_cap_cannot_exceed_cumulative_turn_replay(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_RESULT_BYTES", "100000")
    monkeypatch.setenv("AGENT_MAX_TURN_REPLAY_BYTES", "65536")
    with pytest.raises(ConfigurationError, match="must not exceed"):
        _reload()


def test_reviewed_spreadsheet_default_is_full_bounded_batch(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_SPREADSHEET_ROWS", raising=False)
    assert _reload().agent_max_spreadsheet_rows == 500
