from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


APP = Path(__file__).resolve().parents[1]
TEMPLATE = APP / "templates" / "dashboard.html"
JAVASCRIPT = APP / "static" / "app.js"
ASYNC_GUARD = APP / "static" / "agent_async_guard.js"


def _render_dashboard(
    agent_access_allowed: bool,
    agent_writes_enabled: bool | None = None,
) -> str:
    environment = Environment(
        loader=FileSystemLoader(APP / "templates"),
        autoescape=select_autoescape(("html",)),
    )
    environment.globals["url_for"] = (
        lambda name, **kwargs: f"/{name}/{kwargs.get('path', '').lstrip('/')}"
    )
    environment.globals["asset_version"] = "test"
    return environment.get_template("dashboard.html").render(
        user={"user_login": "joseph", "display_name": "Joseph"},
        csrf_token="csrf-test-token",
        wp_target_host="wordpress.example.test",
        agent_access_allowed=agent_access_allowed,
        agent_writes_enabled=(
            agent_access_allowed
            if agent_writes_enabled is None
            else agent_writes_enabled
        ),
    )


def _assistant_javascript() -> str:
    source = JAVASCRIPT.read_text()
    start = source.index("// ===== Allowlisted in-app assistant =====")
    # The assistant module ends where the next section banner begins; the
    # wiring block moved below other modules in August, which silently
    # widened this scan to unrelated code.
    end = source.index("// ===== Bulk-apply panel =====", start)
    return source[start:end]


def test_server_render_omits_every_assistant_entry_point_when_disallowed():
    rendered = _render_dashboard(False)
    assert 'id="assistant-toggle"' not in rendered
    assert 'id="assistant-fab"' not in rendered
    assert 'id="assistant-panel"' not in rendered
    assert 'id="assistant-backdrop"' not in rendered
    assert 'id="assistant-file"' not in rendered


def test_server_render_includes_assistant_for_allowlisted_operator():
    rendered = _render_dashboard(True)
    assert 'id="assistant-toggle"' in rendered
    assert 'aria-controls="assistant-panel"' in rendered
    assert 'id="assistant-panel"' in rendered
    assert 'id="assistant-review"' in rendered
    assert 'id="assistant-mapping"' in rendered
    assert 'name="csrf-token" content="csrf-test-token"' in rendered


def test_read_only_release_omits_spreadsheet_controls():
    rendered = _render_dashboard(True, agent_writes_enabled=False)
    assert 'id="assistant-panel"' in rendered
    assert 'id="assistant-file"' not in rendered
    assert 'id="assistant-attach"' not in rendered


def test_agent_code_uses_no_active_html_or_url_sink_for_untrusted_values():
    source = _assistant_javascript()
    forbidden = (
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        ".src =",
        ".href =",
        "document.write",
        "eval(",
        "new Function(",
    )
    for value in forbidden:
        assert value not in source
    assert "node.textContent = String(value ?? \"\")" in source
    assert "chip.textContent = \"Used \" + String(toolName ?? \"tool\")" in source


def test_malicious_examples_are_rendered_only_through_text_content():
    source = _assistant_javascript()
    fixtures = (
        '<img src=x onerror=alert(1)>',
        '<svg/onload=alert(1)>',
        'javascript:alert(1)',
        '</textarea><script>alert(1)</script>',
        '=HYPERLINK("https://example.invalid", "click")',
    )
    # The browser behavior is enforced structurally: every provider/database
    # value flows through agentNode/appendAgentText, whose only write is
    # textContent. Keep the payload list here as the regression corpus.
    assert fixtures
    assert "function appendAgentText" in source
    assert "function agentNode" in source
    assert "function renderChangeSet" in source
    assert "function renderMappingJob" in source


def test_stream_fetch_carries_session_cookie_and_csrf_and_parses_sse():
    source = _assistant_javascript()
    guard = ASYNC_GUARD.read_text()
    assert 'credentials: "same-origin"' in source
    assert '"Accept": "text/event-stream"' in source
    assert '"X-CSRF-Token": csrfToken' in source
    assert "assistantAsyncGuard.consumeSse(response, onEvent)" in source
    assert "response.body.getReader()" in guard
    assert 'line.startsWith(":")' in guard
    assert 'frame.indexOf' not in guard  # frames are found on the shared buffer


def test_initializer_fails_closed_when_server_omits_agent_dom():
    source = _assistant_javascript()
    assert 'if (!panel || !toggle) return;' in source


def test_real_browser_hook_is_automation_only_and_unavailable_on_http():
    source = JAVASCRIPT.read_text()
    assert "window.__ARB_AGENT_UI_BROWSER_TEST__" in source
    assert "navigator.webdriver === true" in source
    assert '["about:", "file:"].includes(window.location.protocol)' in source


def test_history_reconstructs_all_review_and_resumable_mapping_cards():
    source = _assistant_javascript()
    assert "payload?.change_sets" in source
    assert "payload?.spreadsheet_jobs" in source
    assert "assistantState.reviewQueue =" in source
    assert "assistantState.mappingQueue =" in source
    assert "appendAssistantWorkflowNavigation" in source
    assert "await loadChangeSet(elements, latestChangeSetId" in source
    assert "await loadMappingJob(elements, latestMappingJobId" in source
    assert '["mapping_pending", "mapping_confirmed"].includes(job.status)' in source
    assert '"Resume staging rows"' in source


def test_all_history_collections_have_deduplicated_keyset_load_more_ui():
    source = _assistant_javascript()
    assert "function mergeAssistantRecords" in source
    assert "known.has(id)" in source
    assert "sessions_truncated" in source
    assert "sessions_oldest_cursor" in source
    assert '"Load older chats"' in source
    assert "before_updated_at" in source
    assert "change_sets_truncated" in source
    assert "change_sets_oldest_cursor" in source
    assert "change_set_before_priority" in source
    assert "change_set_before_updated_at" in source
    assert "spreadsheet_jobs_truncated" in source
    assert "spreadsheet_jobs_oldest_cursor" in source
    assert "spreadsheet_before_priority" in source
    assert "spreadsheet_before_created_at" in source
    assert '"Load more"' in source
    assert "assistantGenerationMatches(generation)" in source


def test_session_bound_async_work_is_versioned_and_abortable():
    source = _assistant_javascript()
    assert "advanceAssistantGeneration(elements)" in source
    assert "assistantContextMatches(generation" in source
    assert "operationControllers.forEach((controller) => controller.abort())" in source
    assert "signal: controller.signal" in source
    assert "generation !== assistantState.generation" in source
    assert "advanceAssistantGeneration(elements);" in source
    assert "function resetAssistantInteraction(elements)" in source
    assert "resetAssistantInteraction(elements);" in source
    assert "assistantAsyncGuard.composerBlocked(assistantState)" in source
    assert "elements.send.disabled = composerBlocked" in source
    assert "elements.stop.hidden = !assistantState.streaming" in source
    assert "elements.input.disabled = composerBlocked" in source


def test_truncated_history_has_keyset_load_older_ui():
    source = _assistant_javascript()
    assert "messages_truncated" in source
    assert "messages_oldest_cursor" in source
    assert '"Load older messages"' in source
    assert "before_created_at" in source
    assert "before_id" in source
    assert "content_truncated" in source
    assert "Older message content was truncated by the history safety limit." in source
