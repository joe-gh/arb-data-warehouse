(function installAgentAsyncGuard(root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ArbAgentAsyncGuard = api;
}(typeof globalThis === "object" ? globalThis : this, function createGuard() {
  "use strict";

  const KEYS = Object.freeze([
    "message",
    "changeSet",
    "mapping",
    "reviewPage",
    "mappingPage",
  ]);

  function assertKey(key) {
    if (!KEYS.includes(key)) throw new TypeError("unknown assistant guard key");
  }

  function create() {
    return Object.fromEntries(KEYS.map((key) => [key, 0]));
  }

  function begin(guard, key) {
    assertKey(key);
    guard[key] = Number(guard[key] || 0) + 1;
    return guard[key];
  }

  function current(guard, key, token) {
    assertKey(key);
    return Number(guard[key]) === Number(token);
  }

  function invalidateAll(guard) {
    KEYS.forEach((key) => begin(guard, key));
  }

  function composerBlocked(state) {
    return Boolean(
      state.streaming
      || state.sessionLoading
      || state.messageHistoryLoading
    );
  }

  function recordId(record) {
    if (!record || typeof record !== "object") return "";
    return String(
      record.id
      || record.session_id
      || record.change_set_id
      || record.job_id
      || "",
    );
  }

  function upsertRecord(records, record) {
    const id = recordId(record);
    if (!id) return Array.isArray(records) ? [...records] : [];
    const current = Array.isArray(records) ? records : [];
    const index = current.findIndex((item) => recordId(item) === id);
    if (index < 0) return [record, ...current];
    return current.map((item, itemIndex) => (
      itemIndex === index ? record : item
    ));
  }

  function beginSessionLoad(state, messages, loadingNode) {
    state.sessionLoading = true;
    state.messages = [];
    state.messagesTruncated = false;
    state.messagesOldestCursor = null;
    messages.replaceChildren(loadingNode);
  }

  function rememberTurn(state, userContent, assistantContent, turnKey) {
    const key = String(turnKey || "local-turn");
    state.messages.push(
      {
        id: `${key}:user`,
        role: "user",
        status: "complete",
        content: String(userContent || ""),
      },
      {
        id: `${key}:assistant`,
        role: "assistant",
        status: "complete",
        content: String(assistantContent || ""),
      },
    );
  }

  function mergeOlderMessages(state, payload) {
    const older = Array.isArray(payload?.messages) ? payload.messages : [];
    const existingIds = new Set(state.messages.map((item) => String(item?.id || "")));
    state.messages = [
      ...older.filter((item) => !existingIds.has(String(item?.id || ""))),
      ...state.messages,
    ];
    state.messagesTruncated = Boolean(payload?.messages_truncated);
    state.messagesOldestCursor = payload?.messages_oldest_cursor ?? null;
  }

  async function consumeSse(response, onEvent) {
    if (!response.body) throw new Error("Streaming is not supported by this browser.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", {fatal: false});
    let buffer = "";
    let finished = false;

    function consumeFrame(frame) {
      const dataLines = [];
      frame.split("\n").forEach((rawLine) => {
        const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
        if (!line || line.startsWith(":")) return;
        if (line === "data") dataLines.push("");
        else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).replace(/^ /, ""));
        }
      });
      if (!dataLines.length) return;
      let event;
      try {
        event = JSON.parse(dataLines.join("\n"));
      } catch {
        throw new Error("The assistant returned an invalid stream event.");
      }
      if (!event || typeof event !== "object" || Array.isArray(event)) {
        throw new Error("The assistant returned an invalid stream event.");
      }
      onEvent(event);
    }

    try {
      while (true) {
        const {value, done} = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
        buffer = buffer.replaceAll("\r\n", "\n");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          consumeFrame(frame);
        }
        if (done) {
          finished = true;
          break;
        }
      }
      if (buffer.trim()) consumeFrame(buffer);
    } finally {
      if (!finished) {
        try {
          await reader.cancel();
        } catch {
          // A failed/disconnected browser stream can already be closed.
        }
      }
    }
  }

  return Object.freeze({
    KEYS,
    create,
    begin,
    current,
    invalidateAll,
    composerBlocked,
    upsertRecord,
    beginSessionLoad,
    rememberTurn,
    mergeOlderMessages,
    consumeSse,
  });
}));
