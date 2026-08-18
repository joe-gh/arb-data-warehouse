"""Real-browser race coverage for the production assistant UI closure."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


APP = Path(__file__).resolve().parents[1]
APP_SCRIPT = APP / "static" / "app.js"
GUARD_SCRIPT = APP / "static" / "agent_async_guard.js"

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
MESSAGE_1 = "31111111-1111-4111-8111-111111111111"
MESSAGE_2 = "32222222-2222-4222-8222-222222222222"
MESSAGE_0 = "30000000-0000-4000-8000-000000000000"
CHANGE_SETS = tuple(
    f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}"
    for index in range(1, 5)
)
MAPPING_JOBS = tuple(
    f"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb{index}"
    for index in range(1, 5)
)
HASH = "a" * 64


ASSISTANT_DOCUMENT = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="csrf-token" content="browser-test-csrf">
</head>
<body>
  <div id="toast-region"></div>
  <button id="assistant-toggle" type="button" aria-expanded="false">Assistant</button>
  <div id="assistant-backdrop" hidden></div>
  <aside id="assistant-panel" data-writes-enabled="true" hidden aria-hidden="true">
    <button id="assistant-close" type="button">Close</button>
    <button id="assistant-new" type="button">New chat</button>
    <button id="assistant-history-toggle" type="button" aria-expanded="false">Show</button>
    <div id="assistant-session-list" hidden></div>
    <div id="assistant-status"></div>
    <div id="assistant-messages"><div class="assistant-empty">Empty</div></div>
    <section id="assistant-review" hidden></section>
    <section id="assistant-mapping" hidden></section>
    <form id="assistant-form">
      <textarea id="assistant-input"></textarea>
      <button id="assistant-send" type="submit">Send</button>
      <button id="assistant-stop" type="button" hidden>Stop</button>
      <button id="assistant-attach" type="button">Attach</button>
      <input id="assistant-file" type="file">
      <input id="assistant-spreadsheet-instruction" type="text">
    </form>
  </aside>
</body>
</html>
"""


INSTALL_FETCH_BROKER = r"""
() => {
  window.__ARB_AGENT_UI_BROWSER_TEST__ = {assistantOnly: true};
  const requests = [];
  let sequence = 0;
  let nextSseHonorsAbort = true;

  function find(id) {
    const request = requests.find((item) => item.id === id);
    if (!request) throw new Error(`Unknown request ${id}`);
    return request;
  }

  window.__ARB_BROWSER_FETCH__ = {
    requests,
    setNextSseHonorsAbort(value) {
      nextSseHonorsAbort = Boolean(value);
    },
    respond(id, payload, status = 200) {
      const request = find(id);
      request.responded = true;
      request.resolve(new Response(JSON.stringify(payload), {
        status,
        headers: {"content-type": "application/json"},
      }));
    },
    push(id, event) {
      const request = find(id);
      request.controller.enqueue(new TextEncoder().encode(
        `data: ${JSON.stringify(event)}\n\n`,
      ));
    },
    end(id) {
      const request = find(id);
      request.controller.close();
    },
  };

  window.fetch = (input, init = {}) => {
    const request = {
      id: ++sequence,
      url: String(input),
      method: String(init.method || "GET").toUpperCase(),
      claimed: false,
      aborted: false,
      responded: false,
      kind: "json",
    };
    const signal = init.signal;
    const isSse = request.url.endsWith("/api/agent/chat");
    if (isSse) {
      request.kind = "sse";
      request.honorsAbort = nextSseHonorsAbort;
      nextSseHonorsAbort = true;
      const body = new ReadableStream({
        start(controller) { request.controller = controller; },
      });
      if (signal) signal.addEventListener("abort", () => {
        request.aborted = true;
        if (request.honorsAbort) {
          request.controller.error(new DOMException("Aborted", "AbortError"));
        }
      }, {once: true});
      requests.push(request);
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: {"content-type": "text/event-stream"},
      }));
    }

    if (signal) signal.addEventListener("abort", () => {
      // Deliberately record but do not reject. Tests can deliver a response
      // after AbortSignal to prove production generation/session fencing.
      request.aborted = true;
    }, {once: true});
    const promise = new Promise((resolve) => { request.resolve = resolve; });
    requests.push(request);
    return promise;
  };
}
"""


def _matches_expression() -> str:
    return """
    ({exact, contains, method}) => window.__ARB_BROWSER_FETCH__.requests.some(
      (request) => !request.claimed
        && (!exact || request.url === exact)
        && (!contains || request.url.includes(contains))
        && (!method || request.method === method)
    )
    """


def _take_request(page, *, exact=None, contains=None, method=None) -> dict:
    criteria = {"exact": exact, "contains": contains, "method": method}
    page.wait_for_function(_matches_expression(), arg=criteria, timeout=5_000)
    return page.evaluate(
        """
        ({exact, contains, method}) => {
          const request = window.__ARB_BROWSER_FETCH__.requests.find(
            (item) => !item.claimed
              && (!exact || item.url === exact)
              && (!contains || item.url.includes(contains))
              && (!method || item.method === method)
          );
          request.claimed = true;
          return {
            id: request.id,
            url: request.url,
            method: request.method,
            kind: request.kind,
          };
        }
        """,
        criteria,
    )


def _respond(page, request: dict, payload: dict, status: int = 200) -> None:
    page.evaluate(
        "([id, payload, status]) => "
        "window.__ARB_BROWSER_FETCH__.respond(id, payload, status)",
        [request["id"], payload, status],
    )


def _push(page, request: dict, event: dict) -> None:
    page.evaluate(
        "([id, event]) => window.__ARB_BROWSER_FETCH__.push(id, event)",
        [request["id"], event],
    )


def _end(page, request: dict) -> None:
    page.evaluate(
        "id => window.__ARB_BROWSER_FETCH__.end(id)",
        request["id"],
    )


def _snapshot(page) -> dict:
    return page.evaluate("window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot()")


def _flush_browser(page) -> None:
    """Let response-body and guarded continuation microtasks reach the DOM."""

    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame("
        "() => requestAnimationFrame(resolve)))"
    )


def _wait_for_idle(page) -> None:
    page.wait_for_function(
        """
        () => {
          const state = window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot();
          return !state.streaming
            && !state.sessionLoading
            && !state.messageHistoryLoading
            && state.operationCount === 0;
        }
        """,
        timeout=5_000,
    )


def _session_payload(
    session_id: str,
    *,
    messages=(),
    messages_truncated=False,
    messages_oldest_cursor=None,
    change_sets=(),
    change_sets_truncated=False,
    change_sets_oldest_cursor=None,
    spreadsheet_jobs=(),
    spreadsheet_jobs_truncated=False,
    spreadsheet_jobs_oldest_cursor=None,
) -> dict:
    return {
        "session": {"id": session_id, "title": f"Session {session_id[:4]}"},
        "messages": list(messages),
        "messages_truncated": messages_truncated,
        "messages_oldest_cursor": messages_oldest_cursor,
        "change_sets": list(change_sets),
        "change_sets_truncated": change_sets_truncated,
        "change_sets_oldest_cursor": change_sets_oldest_cursor,
        "spreadsheet_jobs": list(spreadsheet_jobs),
        "spreadsheet_jobs_truncated": spreadsheet_jobs_truncated,
        "spreadsheet_jobs_oldest_cursor": spreadsheet_jobs_oldest_cursor,
    }


def _change_set_record(change_set_id: str, status: str = "pending") -> dict:
    return {
        "id": change_set_id,
        "status": status,
        "expires_at": "2099-01-01T00:00:00Z",
        "updated_at": "2026-07-17T12:00:00Z",
    }


def _change_set_detail(change_set_id: str, marker: str) -> dict:
    return {
        "change_set": {
            "id": change_set_id,
            "status": "pending",
            "revision": 1,
            "preview_hash": HASH,
            "contains_hard_delete": False,
            "affected_scopes": [marker],
            "preview_diff": {"marker": marker},
        },
        "items": [{"tool_name": "update_store_settings", "arguments": {"marker": marker}}],
    }


def _mapping_record(job_id: str) -> dict:
    return {
        "id": job_id,
        "status": "mapping_pending",
        "created_at": "2026-07-17T12:00:00Z",
    }


def _mapping_detail(job_id: str, marker: str) -> dict:
    return {
        "id": job_id,
        "status": "mapping_pending",
        "mapping_revision": 1,
        "mapping_hash": HASH,
        "original_name": marker,
        "mapping": {
            "command": "set_store_pricing_tier",
            "columns": {"note": "=HYPERLINK(\"javascript:alert(1)\")"},
        },
        "rejected_rows": [],
    }


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def assistant_page(browser):
    page = browser.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.set_content(ASSISTANT_DOCUMENT)
    page.evaluate(INSTALL_FETCH_BROKER)
    page.add_script_tag(path=str(GUARD_SCRIPT))
    page.add_script_tag(path=str(APP_SCRIPT))
    assert page.evaluate(
        "typeof window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot === 'function'"
    )
    yield page
    page.close()
    assert page_errors == []


def _open_sessions(page, sessions: list[dict]) -> None:
    page.click("#assistant-toggle")
    request = _take_request(
        page,
        exact="/api/agent/sessions",
        method="GET",
    )
    _respond(page, request, {
        "sessions": sessions,
        "sessions_truncated": False,
        "sessions_oldest_cursor": None,
    })
    page.wait_for_function(
        "count => document.querySelectorAll('#assistant-session-list button').length === count",
        arg=len(sessions),
    )
    page.click("#assistant-history-toggle")
    page.locator("#assistant-session-list").wait_for(state="visible")


def _select_simple_session(page, title: str, session_id: str, payload: dict) -> None:
    page.get_by_role("button", name=title, exact=True).click()
    request = _take_request(
        page,
        contains=f"/api/agent/sessions/{session_id}",
        method="GET",
    )
    _respond(page, request, payload)
    page.wait_for_function(
        "id => { const s = window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot(); "
        "return s.sessionId === id && !s.sessionLoading; }",
        arg=session_id,
    )


def test_switching_sessions_mid_sse_fences_late_tokens_and_restores_controls(
    assistant_page,
):
    page = assistant_page
    _open_sessions(page, [
        {"id": SESSION_A, "title": "Session A"},
        {"id": SESSION_B, "title": "Session B"},
    ])
    _select_simple_session(
        page,
        "Session A",
        SESSION_A,
        _session_payload(SESSION_A),
    )

    page.evaluate("window.__ARB_BROWSER_FETCH__.setNextSseHonorsAbort(false)")
    page.fill("#assistant-input", "stream in session A")
    page.click("#assistant-send")
    stream = _take_request(page, exact="/api/agent/chat", method="POST")
    hostile = "<svg/onload=alert(1)>"
    _push(page, stream, {
        "type": "token",
        "text": hostile,
        "session_id": SESSION_A,
    })
    page.get_by_text(hostile, exact=True).wait_for()
    assert page.locator("#assistant-messages svg").count() == 0

    page.click("#assistant-history-toggle")
    page.get_by_role("button", name="Session B", exact=True).click()
    session_b = _take_request(
        page,
        contains=f"/api/agent/sessions/{SESSION_B}",
        method="GET",
    )
    _push(page, stream, {"type": "token", "text": "LATE SESSION A"})
    _respond(page, session_b, _session_payload(
        SESSION_B,
        messages=[{
            "id": MESSAGE_1,
            "role": "assistant",
            "status": "complete",
            "content": "javascript:alert(1) is inert session B text",
        }],
    ))
    _end(page, stream)
    _flush_browser(page)
    _wait_for_idle(page)

    assert _snapshot(page)["sessionId"] == SESSION_B
    assert "LATE SESSION A" not in page.locator("#assistant-messages").inner_text()
    assert "inert session B text" in page.locator("#assistant-messages").inner_text()
    assert page.locator("#assistant-messages a").count() == 0
    assert page.locator("#assistant-input").is_enabled()
    assert page.locator("#assistant-send").is_enabled()
    assert page.locator("#assistant-stop").is_hidden()
    assert page.evaluate(
        "id => window.__ARB_BROWSER_FETCH__.requests.find(r => r.id === id).aborted",
        stream["id"],
    ) is True


def test_stop_abort_uses_production_stream_cleanup_and_reenables_composer(
    assistant_page,
):
    page = assistant_page
    _open_sessions(page, [{"id": SESSION_A, "title": "Session A"}])
    _select_simple_session(
        page,
        "Session A",
        SESSION_A,
        _session_payload(SESSION_A),
    )
    page.fill("#assistant-input", "stop this response")
    page.click("#assistant-send")
    stream = _take_request(page, exact="/api/agent/chat", method="POST")
    _push(page, stream, {"type": "token", "text": "partial"})
    page.get_by_text("partial", exact=True).wait_for()
    page.click("#assistant-stop")
    _wait_for_idle(page)

    assert page.locator("#assistant-status").inner_text() == "Response stopped."
    assert page.locator("#assistant-input").is_enabled()
    assert page.locator("#assistant-send").is_enabled()
    assert page.locator("#assistant-stop").is_hidden()
    assert page.evaluate(
        "id => window.__ARB_BROWSER_FETCH__.requests.find(r => r.id === id).aborted",
        stream["id"],
    ) is True


def test_late_detail_upload_and_apply_from_session_a_cannot_mutate_session_b_dom(
    assistant_page,
):
    page = assistant_page
    _open_sessions(page, [
        {"id": SESSION_A, "title": "Session A"},
        {"id": SESSION_B, "title": "Session B"},
    ])
    page.get_by_role("button", name="Session A", exact=True).click()
    session_a = _take_request(
        page,
        contains=f"/api/agent/sessions/{SESSION_A}",
        method="GET",
    )
    _respond(page, session_a, _session_payload(
        SESSION_A,
        change_sets=[
            _change_set_record(CHANGE_SETS[0]),
            _change_set_record(CHANGE_SETS[1]),
        ],
    ))
    initial_detail = _take_request(
        page,
        exact=f"/api/agent/change-sets/{CHANGE_SETS[0]}",
        method="GET",
    )
    _respond(page, initial_detail, _change_set_detail(
        CHANGE_SETS[0],
        "SESSION A CURRENT",
    ))
    page.wait_for_function(
        "() => !window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot().sessionLoading"
    )

    page.get_by_role("button", name="2. pending", exact=True).click()
    late_detail = _take_request(
        page,
        exact=f"/api/agent/change-sets/{CHANGE_SETS[1]}",
        method="GET",
    )
    page.get_by_role("button", name="Confirm changes", exact=True).click()
    late_apply = _take_request(
        page,
        exact=f"/api/agent/change-sets/{CHANGE_SETS[0]}/apply",
        method="POST",
    )
    page.set_input_files("#assistant-file", {
        "name": "session-a.csv",
        "mimeType": "text/csv",
        "buffer": b"fdm4_store,tier_name\nS_A,MSRP\n",
    })
    late_upload = _take_request(
        page,
        exact="/api/agent/spreadsheets",
        method="POST",
    )

    page.click("#assistant-history-toggle")
    page.get_by_role("button", name="Session B", exact=True).click()
    session_b = _take_request(
        page,
        contains=f"/api/agent/sessions/{SESSION_B}",
        method="GET",
    )
    _respond(page, session_b, _session_payload(
        SESSION_B,
        messages=[{
            "id": MESSAGE_2,
            "role": "assistant",
            "status": "complete",
            "content": "SESSION B DOM",
        }],
    ))
    page.wait_for_function(
        "id => { const s = window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot(); "
        "return s.sessionId === id && !s.sessionLoading; }",
        arg=SESSION_B,
    )

    _respond(page, late_detail, _change_set_detail(
        CHANGE_SETS[1],
        "STALE DETAIL FROM A",
    ))
    _respond(page, late_apply, {"status": "applied"})
    _respond(page, late_upload, _mapping_detail(
        MAPPING_JOBS[0],
        "STALE UPLOAD FROM A",
    ))
    _flush_browser(page)
    _wait_for_idle(page)

    assert _snapshot(page)["sessionId"] == SESSION_B
    rendered = page.locator("#assistant-panel").inner_text()
    assert "SESSION B DOM" in rendered
    assert "STALE DETAIL FROM A" not in rendered
    assert "STALE UPLOAD FROM A" not in rendered
    assert page.locator("#assistant-review").is_hidden()
    assert page.locator("#assistant-mapping").is_hidden()
    assert "applied" not in page.locator("#toast-region").inner_text().lower()
    for request in (late_detail, late_apply, late_upload):
        assert page.evaluate(
            "id => window.__ARB_BROWSER_FETCH__.requests.find(r => r.id === id).aborted",
            request["id"],
        ) is True


def test_load_older_prepends_deduplicates_and_keeps_completed_live_turn(
    assistant_page,
):
    page = assistant_page
    _open_sessions(page, [{"id": SESSION_A, "title": "Session A"}])
    current_user = {
        "id": MESSAGE_1,
        "role": "user",
        "status": "complete",
        "content": "current user",
    }
    current_assistant = {
        "id": MESSAGE_2,
        "role": "assistant",
        "status": "complete",
        "content": "=HYPERLINK(\"javascript:alert(1)\")",
    }
    _select_simple_session(
        page,
        "Session A",
        SESSION_A,
        _session_payload(
            SESSION_A,
            messages=[current_user, current_assistant],
            messages_truncated=True,
            messages_oldest_cursor={
                "id": MESSAGE_1,
                "created_at": "2026-07-17T12:00:00Z",
            },
        ),
    )

    page.fill("#assistant-input", "live user")
    page.click("#assistant-send")
    stream = _take_request(page, exact="/api/agent/chat", method="POST")
    _push(page, stream, {
        "type": "token",
        "text": "live assistant",
        "session_id": SESSION_A,
    })
    _push(page, stream, {"type": "done", "session_id": SESSION_A})
    _end(page, stream)
    _wait_for_idle(page)

    page.get_by_role("button", name="Load older messages", exact=True).click()
    older = _take_request(
        page,
        contains="before_created_at=",
        method="GET",
    )
    malicious_older = "</textarea><script>alert(1)</script>"
    _respond(page, older, {
        "messages": [{
            "id": MESSAGE_0,
            "role": "assistant",
            "status": "complete",
            "content": malicious_older,
        }, current_user],
        "messages_truncated": False,
        "messages_oldest_cursor": None,
    })
    _wait_for_idle(page)

    contents = page.locator("#assistant-messages .assistant-message__content").all_inner_texts()
    assert contents == [
        malicious_older,
        "current user",
        '=HYPERLINK("javascript:alert(1)")',
        "live user",
        "live assistant",
    ]
    assert contents.count("current user") == 1
    assert page.locator("#assistant-messages script").count() == 0
    assert page.locator("#assistant-messages a").count() == 0
    assert page.get_by_role("button", name="Load older messages").count() == 0


def test_every_review_and_mapping_card_is_navigable_after_keyset_pagination(
    assistant_page,
):
    page = assistant_page
    _open_sessions(page, [{"id": SESSION_A, "title": "Session A"}])
    page.get_by_role("button", name="Session A", exact=True).click()
    session = _take_request(
        page,
        contains=f"/api/agent/sessions/{SESSION_A}",
        method="GET",
    )
    _respond(page, session, _session_payload(
        SESSION_A,
        change_sets=[
            _change_set_record(CHANGE_SETS[0]),
            _change_set_record(CHANGE_SETS[1]),
        ],
        change_sets_truncated=True,
        change_sets_oldest_cursor={
            "priority": 1,
            "id": CHANGE_SETS[1],
            "updated_at": "2026-07-17T12:00:00Z",
        },
        spreadsheet_jobs=[
            _mapping_record(MAPPING_JOBS[0]),
            _mapping_record(MAPPING_JOBS[1]),
        ],
        spreadsheet_jobs_truncated=True,
        spreadsheet_jobs_oldest_cursor={
            "priority": 1,
            "id": MAPPING_JOBS[1],
            "created_at": "2026-07-17T12:00:00Z",
        },
    ))
    initial_change = _take_request(
        page,
        exact=f"/api/agent/change-sets/{CHANGE_SETS[0]}",
        method="GET",
    )
    _respond(page, initial_change, _change_set_detail(
        CHANGE_SETS[0],
        "CHANGE 1",
    ))
    initial_mapping = _take_request(
        page,
        exact=f"/api/agent/spreadsheets/{MAPPING_JOBS[0]}",
        method="GET",
    )
    _respond(page, initial_mapping, _mapping_detail(
        MAPPING_JOBS[0],
        "MAPPING 1",
    ))
    page.wait_for_function(
        "() => !window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot().sessionLoading"
    )

    page.locator("#assistant-review").get_by_role(
        "button", name="Load more", exact=True
    ).click()
    review_page = _take_request(
        page,
        contains="change_set_before_priority=",
        method="GET",
    )
    _respond(page, review_page, {
        "change_sets": [
            _change_set_record(CHANGE_SETS[2]),
            _change_set_record(CHANGE_SETS[3]),
        ],
        "change_sets_truncated": False,
        "change_sets_oldest_cursor": None,
    })
    refreshed_change = _take_request(
        page,
        exact=f"/api/agent/change-sets/{CHANGE_SETS[0]}",
        method="GET",
    )
    _respond(page, refreshed_change, _change_set_detail(
        CHANGE_SETS[0],
        "CHANGE 1",
    ))
    page.locator("#assistant-review").get_by_role(
        "button", name="4. pending", exact=True
    ).wait_for()
    assert len(_snapshot(page)["reviewQueue"]) == 4

    page.locator("#assistant-mapping").get_by_role(
        "button", name="Load more", exact=True
    ).click()
    mapping_page = _take_request(
        page,
        contains="spreadsheet_before_priority=",
        method="GET",
    )
    _respond(page, mapping_page, {
        "spreadsheet_jobs": [
            _mapping_record(MAPPING_JOBS[2]),
            _mapping_record(MAPPING_JOBS[3]),
        ],
        "spreadsheet_jobs_truncated": False,
        "spreadsheet_jobs_oldest_cursor": None,
    })
    refreshed_mapping = _take_request(
        page,
        exact=f"/api/agent/spreadsheets/{MAPPING_JOBS[0]}",
        method="GET",
    )
    _respond(page, refreshed_mapping, _mapping_detail(
        MAPPING_JOBS[0],
        "MAPPING 1",
    ))
    page.locator("#assistant-mapping").get_by_role(
        "button", name="4. mapping pending", exact=True
    ).wait_for()
    assert len(_snapshot(page)["mappingQueue"]) == 4

    for index, change_set_id in enumerate(CHANGE_SETS, start=1):
        if _snapshot(page)["changeSet"]["id"] != change_set_id:
            page.locator("#assistant-review").get_by_role(
                "button", name=f"{index}. pending", exact=True
            ).click()
            request = _take_request(
                page,
                exact=f"/api/agent/change-sets/{change_set_id}",
                method="GET",
            )
            marker = "<img src=x onerror=alert(1)>" if index == 4 else f"CHANGE {index}"
            _respond(page, request, _change_set_detail(change_set_id, marker))
            page.wait_for_function(
                "id => window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot().changeSet.id === id",
                arg=change_set_id,
            )
        assert _snapshot(page)["changeSet"]["id"] == change_set_id

    for index, job_id in enumerate(MAPPING_JOBS, start=1):
        if _snapshot(page)["mappingJob"]["id"] != job_id:
            page.locator("#assistant-mapping").get_by_role(
                "button", name=f"{index}. mapping pending", exact=True
            ).click()
            request = _take_request(
                page,
                exact=f"/api/agent/spreadsheets/{job_id}",
                method="GET",
            )
            _respond(page, request, _mapping_detail(job_id, f"MAPPING {index}"))
            page.wait_for_function(
                "id => window.__ARB_AGENT_UI_BROWSER_TEST__.snapshot().mappingJob.id === id",
                arg=job_id,
            )
        assert _snapshot(page)["mappingJob"]["id"] == job_id

    assert page.locator("#assistant-review img").count() == 0
    assert page.locator("#assistant-mapping a").count() == 0
    assert _snapshot(page)["reviewTruncated"] is False
    assert _snapshot(page)["mappingTruncated"] is False
