"""Static and executable checks on the Warehouse Operations front end.

The app is one vanilla-JavaScript file with no build step, so a call to a
helper that does not exist ships silently (the browser only raises when the
line runs, and unawaited callers swallow it). These tests read static/app.js
directly: one scan proves every helper that is called is also defined, the
rest slice single functions out and execute them under node with stubs.
"""

import json
import re
import subprocess
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
JAVASCRIPT = (APP / "static" / "app.js").read_text()


# Words that read like a call site but are language syntax.
JAVASCRIPT_KEYWORDS = {
    "async", "await", "case", "catch", "delete", "do", "else", "for",
    "function", "if", "in", "instanceof", "new", "of", "return", "super",
    "switch", "this", "throw", "try", "typeof", "void", "while", "with",
    "yield",
}

# Browser globals the app calls without declaring them.
BROWSER_GLOBALS = {
    "clearInterval", "clearTimeout", "decodeURIComponent", "encodeURIComponent",
    "fetch", "isFinite", "isNaN", "parseFloat", "parseInt", "queueMicrotask",
    "requestAnimationFrame", "setInterval", "setTimeout", "structuredClone",
}

# A "/" here starts a regular expression, not a division.
REGEX_PRECEDING_PUNCTUATION = set("(,=:[!&|?{};+-*%~^<>")
REGEX_PRECEDING_WORDS = {
    "await", "case", "do", "else", "in", "new", "of", "return", "typeof",
    "yield",
}


def _code_only(source: str) -> str:
    """app.js with comments and literal text blanked out, keeping the code
    inside `${...}` placeholders. Prose in comments contains plenty of
    "word (" that would otherwise read as a call."""

    out: list[str] = []
    # Each entry is [kind, extra]: ("code", brace depth) or ("str", delimiter).
    stack: list[list] = [["code", 0]]
    index, length = 0, len(source)

    def previous_token() -> str:
        text = "".join(out).rstrip()
        if not text:
            return ""
        if text[-1].isalnum() or text[-1] in "_$":
            return re.search(r"[A-Za-z0-9_$]+$", text).group(0)
        return text[-1]

    while index < length:
        state = stack[-1]
        character = source[index]
        pair = source[index:index + 2]
        if state[0] == "code":
            if pair == "//":
                newline = source.find("\n", index)
                index = length if newline < 0 else newline
                continue
            if pair == "/*":
                end = source.find("*/", index + 2)
                index = length if end < 0 else end + 2
                continue
            if character == "/":
                token = previous_token()
                starts_regex = (
                    token == ""
                    or token in REGEX_PRECEDING_WORDS
                    or (len(token) == 1 and token in REGEX_PRECEDING_PUNCTUATION)
                )
                if starts_regex:
                    index += 1
                    in_character_class = False
                    while index < length:
                        current = source[index]
                        if current == "\\":
                            index += 2
                            continue
                        if current == "[":
                            in_character_class = True
                        elif current == "]":
                            in_character_class = False
                        elif current == "/" and not in_character_class:
                            index += 1
                            break
                        elif current == "\n":
                            break
                        index += 1
                    while index < length and source[index].isalpha():
                        index += 1
                    out.append(" ")
                    continue
            if character in "\"'`":
                stack.append(["str", character])
                out.append(" ")
                index += 1
                continue
            if character == "{" and len(stack) > 1:
                state[1] += 1
            elif character == "}" and len(stack) > 1:
                if state[1] == 0:
                    stack.pop()
                    out.append(" ")
                    index += 1
                    continue
                state[1] -= 1
            out.append(character)
            index += 1
            continue
        # Inside a string or template literal.
        if character == "\\":
            index += 2
            continue
        if character == state[1]:
            stack.pop()
            out.append(" ")
            index += 1
            continue
        if state[1] == "`" and pair == "${":
            stack.append(["code", 0])
            out.append(" ")
            index += 2
            continue
        out.append("\n" if character == "\n" else " ")
        index += 1
    return "".join(out)


def _called_names(code: str) -> set[str]:
    names = {
        match.group(1)
        for match in re.finditer(r"(?<![.\w$])([a-z][A-Za-z0-9_$]*)\s*\(", code)
    }
    return names - JAVASCRIPT_KEYWORDS - BROWSER_GLOBALS


def _is_declared(name: str, code: str) -> bool:
    patterns = (
        rf"function\s+{name}\s*\(",              # function declaration
        rf"\b(?:const|let|var)\s+{name}\b",      # binding
        rf"\b{name}\s*[:=]",                     # object property or assignment
        rf"\([^()]*\b{name}\b[^()]*\)\s*(?:=>|\{{)",  # parameter list
    )
    return any(re.search(pattern, code) for pattern in patterns)


def _undefined_calls(code: str) -> list[str]:
    return sorted(name for name in _called_names(code) if not _is_declared(name, code))


def _run_node(program: str) -> None:
    result = subprocess.run(
        ["node", "-e", program], text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def _slice(start: str, end: str) -> str:
    return JAVASCRIPT[JAVASCRIPT.index(start):JAVASCRIPT.index(end)]


PRELUDE = """
const assert = require('node:assert/strict');
const text = (value, fallback = "") => (value === null || value === undefined ? fallback : String(value));
const escapeHtml = (value) => text(value).replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[char]);
"""


def test_every_called_helper_is_defined():
    """The Category Products view shipped calling catNodeOptionsHtml(), which
    was defined nowhere; the caller was unawaited so the ReferenceError never
    surfaced."""

    assert _undefined_calls(_code_only(JAVASCRIPT)) == []


def test_the_undefined_call_scan_would_have_caught_the_products_view():
    """Proof the scan has teeth: remove the helper the Products view needs and
    the same scan names it."""

    code = _code_only(JAVASCRIPT)
    without_helper = re.sub(
        r"\n\s*function catNodeOptionsHtml\(\)\s*\{.*?\n\s*\}\n",
        "\n",
        code,
        count=1,
        flags=re.S,
    )
    assert "function catNodeOptionsHtml(" not in without_helper
    assert "catNodeOptionsHtml" in _undefined_calls(without_helper)


def test_category_products_dispatcher_reports_a_failed_load():
    source = JAVASCRIPT
    assert 'if (tab === "products") loadCatProducts().catch(' in source
    assert 'select.innerHTML = catNodeOptionsHtml();' in source


def test_node_options_are_full_paths_sorted_and_escaped():
    nodes = [
        {"node_id": 2, "parent_id": 1, "name": "Boots", "slug": "boots"},
        {"node_id": 1, "parent_id": None, "name": "Footwear", "slug": "footwear"},
        {"node_id": 3, "parent_id": 1, "name": "<img src=x onerror=alert(1)>", "slug": "bad"},
    ]
    program = PRELUDE + "const catTreeState = { nodes: " + json.dumps(nodes) + " };\n" + _slice(
        "  function catNodePaths()", "  function openCatMoveDialog()"
    ) + """
const html = catNodeOptionsHtml();
const options = html.match(/<option /g) || [];
assert.equal(options.length, 3);
assert.ok(html.includes('<option value="1">Footwear</option>'));
assert.ok(html.includes('<option value="2">Footwear \\u203a Boots</option>'));
assert.ok(!html.includes('<img src=x'), 'a node name must never reach the select as markup');
assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
const order = ['1', '2', '3'].map((id) => html.indexOf('value="' + id + '"'));
assert.ok(order[0] < order[2] && order[2] < order[1], 'options sort by full path');
"""
    _run_node(program)


# ----- F-31: adding a logo row after a reorder -----


def test_next_option_row_allocates_above_the_highest_identity():
    """Rows are listed in sort_order, so the last row on screen is not the
    highest row number; allocating from it hands the new row an identity that
    already exists."""

    program = PRELUDE + _slice("  function nextOptionRow(", "  function renderGrid(") + """
assert.equal(nextOptionRow([2, 1]), 3);
assert.equal(nextOptionRow([1, 3, 2]), 4);
assert.equal(nextOptionRow([1]), 2);
"""
    _run_node(program)


def test_add_row_refuses_an_occupied_slot_and_asks_the_server_to_insert_only():
    source = JAVASCRIPT
    assert "const nextRow = nextOptionRow(rowNumbers);" in source
    assert 'if (byKey.has(`${code}:${nextRow}:1`))' in source
    assert "if (!assignment) payload.create_only = true;" in source
    # A refused insert must send the person back to a reloaded grid, never
    # retry against the row number the dialog captured.
    assert "That logo row was taken while this editor was open." in source


# ----- F-59: a late names response must not bind to the current store -----


def test_names_rows_carry_the_store_they_were_requested_for():
    rows = [
        {
            "design_id": "D1", "color_scheme_id": "BK", "logo_code": "C52BK",
            "name": "Arborwear", "source": "fdm4", "locked": False,
            "store_specific": True, "fdm4_store": "000111", "art_id": "A1",
            "fdm4_description": "desc",
        }
    ]
    program = PRELUDE + """
const elements = new Map();
const $ = (selector) => {
  if (!elements.has(selector)) elements.set(selector, { textContent: "", innerHTML: "", hidden: false, disabled: false });
  return elements.get(selector);
};
const $$ = () => [];
const namesState = { q: "", filter: "", limit: 50, offset: 0, total: 1, generation: 3 };
const state = { store: "STORE-B" };
const storeDisplayFor = (code) => String(code);
const nameSourceLabel = (source) => String(source);
""" + _slice("  function renderNames(", "  async function saveName(") + """
renderNames(""" + json.dumps(rows) + """, "STORE-A");
const html = $("#names-list").innerHTML;
assert.ok(html.includes('data-rowstore="STORE-A"'), 'rows keep the store they were fetched for');
assert.ok(!html.includes('STORE-B'), 'the live store selection must not label another store\\'s rows');
assert.ok(html.includes('data-override="1"'));
"""
    _run_node(program)


def test_names_load_drops_a_superseded_response():
    source = JAVASCRIPT
    load = _slice("  async function loadNames()", "  function renderNames(")
    assert "const generation = ++namesState.generation;" in load
    assert 'const requestedStore = state.store || "";' in load
    assert "if (generation !== namesState.generation) return;" in load
    assert "renderNames(envelope(resp, \"names\"), requestedStore);" in load
    assert "generation: 0" in source


# ----- F-58: a mix action must stay on the store that was confirmed -----


MIX_STUBS = """
const mixState = { store: "STORE-A", selected: new Set(["S1"]) };
const requests = [];
let flipStoreOnPreview = true;
const api = async (path, options = {}) => {
  requests.push({ path, method: options.method || "GET", body: options.body });
  if (path.includes("preview")) {
    if (flipStoreOnPreview) mixState.store = "STORE-B";
    return { styles_affected: 1, products_retired: 4 };
  }
  return {};
};
const toasts = [];
const toast = (message) => toasts.push(String(message));
const confirmAction = async () => true;
const setBusy = () => {};
const storeDisplayFor = (code) => String(code);
const styleSample = (styles) => [...styles].join(", ");
const mixRefreshStores = async () => {};
const loadMixStyles = () => {};
"""


def test_mix_removal_never_deletes_from_the_store_selected_mid_confirmation():
    program = PRELUDE + MIX_STUBS + _slice(
        "  // The store picker and the store chips stay live", "  async function mixEnable("
    ) + _slice(
        "  async function mixRemoveStyles(", "  async function mixAddStyles("
    ) + """
(async () => {
  await mixRemoveStyles(["S1"], null);
  const deletes = requests.filter((r) => r.method === "DELETE");
  assert.equal(deletes.length, 0, 'no delete may run against a store the person did not confirm');
  assert.ok(toasts.some((t) => t.includes("changed while you were confirming")));

  flipStoreOnPreview = false;
  mixState.store = "STORE-A";
  requests.length = 0;
  await mixRemoveStyles(["S1"], null);
  const confirmed = requests.filter((r) => r.method === "DELETE");
  assert.equal(confirmed.length, 1);
  assert.equal(confirmed[0].body.store, "STORE-A");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    _run_node(program)


def test_every_mix_write_pins_the_store_before_its_first_await():
    source = JAVASCRIPT
    assert "function mixStoreStillSelected(store)" in source
    for start, end in (
        ("  async function mixEnable(", "  function renderMixAll("),
        ("  async function mixExternalToggle(", "  async function mixSwitchMode("),
        ("  async function mixSwitchMode(", "  async function mixDisable("),
        ("  async function mixDisable(", "  function renderMixList("),
        ("  async function mixRemoveStyles(", "  async function mixAddStyles("),
        ("  async function mixImport(", "  // --- Style editor dialog ---"),
    ):
        body = _slice(start, end)
        assert "const store = mixState.store;" in body, start
        assert "mixStoreStillSelected(store)" in body, start
        assert "mixState.store" not in body.split("const store = mixState.store;", 1)[1], start


# ----- F-60: bulk apply must match the preview it is showing -----


BULK_STUBS = """
const inputs = {
  "#bulk-logo-code": { value: "C52BK" },
  "#bulk-logo-scheme": { value: "BK" },
  "#bulk-class": { value: "light" },
  "#bulk-preview-filter": { value: "" },
};
const $ = (selector) => inputs[selector] || null;
let targetMode = "light_dark";
const document = { querySelector: (selector) => (selector.includes(":checked") ? { value: targetMode } : null) };
let tickedColors = ["BLK", "NAV"];
const bulkSelectedColors = () => tickedColors.slice();
const state = { store: "STORE-A" };
const batchState = { selected: new Set(["ST1"]) };
"""


def test_bulk_fingerprint_covers_every_input_the_preview_was_built_from():
    program = PRELUDE + BULK_STUBS + _slice(
        "  // Everything the preview rows depend on.", "  // A preview describes one set of inputs."
    ) + """
const baseline = bulkInputsFingerprint();
assert.equal(bulkInputsFingerprint(), baseline, 'the fingerprint is stable while nothing changes');

// Things the operator can do that must NOT drop the preview.
inputs["#bulk-preview-filter"].value = "hood";
assert.equal(bulkInputsFingerprint(), baseline, 'filtering the preview rows is not an input change');
tickedColors = ["NAV", "BLK"];
assert.equal(bulkInputsFingerprint(), baseline, 'colour order is not an input change');

const changes = [
  ["store", () => { state.store = "STORE-B"; }, () => { state.store = "STORE-A"; }],
  ["logo code", () => { inputs["#bulk-logo-code"].value = "C52WH"; }, () => { inputs["#bulk-logo-code"].value = "C52BK"; }],
  ["colour scheme", () => { inputs["#bulk-logo-scheme"].value = "WH"; }, () => { inputs["#bulk-logo-scheme"].value = "BK"; }],
  ["target mode", () => { targetMode = "colors"; }, () => { targetMode = "light_dark"; }],
  ["light/dark class", () => { inputs["#bulk-class"].value = "dark"; }, () => { inputs["#bulk-class"].value = "light"; }],
  ["batch selection", () => { batchState.selected.add("ST2"); }, () => { batchState.selected.delete("ST2"); }],
];
changes.forEach(([label, change, undo]) => {
  change();
  assert.notEqual(bulkInputsFingerprint(), baseline, label + ' must invalidate the preview');
  undo();
  assert.equal(bulkInputsFingerprint(), baseline, label + ' restores the fingerprint');
});

// In colour mode the ticked colours are the target, so they count.
targetMode = "colors";
const colorBaseline = bulkInputsFingerprint();
tickedColors = ["BLK"];
assert.notEqual(bulkInputsFingerprint(), colorBaseline, 'unticking a colour must invalidate the preview');
"""
    _run_node(program)


def test_bulk_apply_refuses_rows_from_an_obsolete_preview():
    source = JAVASCRIPT
    apply_body = _slice("  async function bulkApply()", "  async function bulkUndo(")
    assert "if (!bulkState.previewKey || bulkState.previewKey !== bulkInputsFingerprint())" in apply_body
    assert apply_body.index("bulkState.previewKey") < apply_body.index("/api/bulk-apply/execute")
    assert "invalidateBulkPreview();" in apply_body
    preview = _slice("  async function bulkPreview()", "  async function bulkApply()")
    assert "const fingerprint = bulkInputsFingerprint();" in preview
    assert "bulkState.previewKey = fingerprint;" in preview
    # Selecting a logo sets the fields in code, which fires no change event.
    assert source.count("invalidateBulkPreview();") >= 4
    assert 'if (bulkState.previewKey && bulkState.previewKey !== bulkInputsFingerprint()) invalidateBulkPreview();' in source
    assert "bulkState.previewKey = null;" in _slice(
        "  async function openBulkApplyPanel()", "  function bulkSelectedColors()"
    )
