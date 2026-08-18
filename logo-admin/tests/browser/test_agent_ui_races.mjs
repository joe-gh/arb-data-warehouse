import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFileSync} from "node:fs";
import test from "node:test";
import {fileURLToPath} from "node:url";
import {dirname, resolve} from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(here, "../..");
const require = createRequire(import.meta.url);
const guardApi = require(resolve(appRoot, "static/agent_async_guard.js"));
const applicationSource = readFileSync(resolve(appRoot, "static/app.js"), "utf8");

function deferred() {
  let resolvePromise;
  const promise = new Promise((resolveValue) => { resolvePromise = resolveValue; });
  return {promise, resolve: resolvePromise};
}

function fakeContainer(...initial) {
  return {
    children: [...initial],
    replaceChildren(...values) { this.children = [...values]; },
    append(value) { this.children.push(value); },
  };
}

test("the production UI is wired to the shared request guard", () => {
  for (const fragment of [
    "assistantAsyncGuard.invalidateAll(assistantRequestGuard)",
    'assistantRequestGuard,\n      "message"',
    'assistantRequestGuard,\n      "changeSet"',
    'assistantRequestGuard,\n      "mapping"',
    "assistantAsyncGuard.beginSessionLoad(",
    "assistantAsyncGuard.consumeSse(response, onEvent)",
    "assistantAsyncGuard.rememberTurn(",
    "assistantAsyncGuard.mergeOlderMessages(assistantState, payload)",
    "assistantAsyncGuard.upsertRecord(",
  ]) assert.ok(applicationSource.includes(fragment), fragment);
});

test("session selection clears old DOM and blocks sending until guarded render", async () => {
  const guard = guardApi.create();
  const state = {
    streaming: false,
    sessionLoading: false,
    messageHistoryLoading: false,
  };
  const messages = fakeContainer("old-session-message");
  const response = deferred();
  let generation = 0;

  async function selectSession() {
    generation += 1;
    const selectedGeneration = generation;
    guardApi.invalidateAll(guard);
    guardApi.beginSessionLoad(state, messages, "Loading previous chat...");
    assert.equal(guardApi.composerBlocked(state), true);
    const payload = await response.promise;
    if (selectedGeneration !== generation) return;
    messages.replaceChildren(...payload.messages);
    state.sessionLoading = false;
  }

  const selection = selectSession();
  assert.deepEqual(messages.children, ["Loading previous chat..."]);
  assert.equal(guardApi.composerBlocked(state), true);
  response.resolve({messages: ["selected-session-message"]});
  await selection;
  assert.deepEqual(messages.children, ["selected-session-message"]);
  assert.equal(guardApi.composerBlocked(state), false);
});

test("a delayed load-older response cannot detach a live SSE message node", async () => {
  const guard = guardApi.create();
  const state = {
    messages: [{id: "current", role: "assistant", content: "current-history"}],
    messagesTruncated: true,
    messagesOldestCursor: {id: "oldest"},
  };
  const messages = fakeContainer("current-history");
  const olderResponse = deferred();
  const olderToken = guardApi.begin(guard, "message");

  const olderRender = olderResponse.promise.then((payload) => {
    if (!guardApi.current(guard, "message", olderToken)) return;
    guardApi.mergeOlderMessages(state, payload);
    messages.replaceChildren(...state.messages.map((item) => item.content));
  });

  const streamToken = guardApi.begin(guard, "message");
  const assistantNode = fakeContainer();
  messages.append("live-user");
  messages.append(assistantNode);
  assert.equal(guardApi.current(guard, "message", streamToken), true);
  let streamController;
  const response = new Response(new ReadableStream({
    start(controller) { streamController = controller; },
  }), {headers: {"content-type": "text/event-stream"}});
  let assistantText = "";
  const streamed = guardApi.consumeSse(response, (event) => {
    if (!guardApi.current(guard, "message", streamToken)) return;
    if (event.type === "token") {
      assistantText += event.text;
      assistantNode.append(event.text);
    }
    if (event.type === "done") {
      guardApi.rememberTurn(state, "live-user", assistantText, "live-turn");
    }
  });
  const encoder = new TextEncoder();
  streamController.enqueue(encoder.encode(
    'data: {"type":"token","text":"first delta"}\n\n',
  ));
  streamController.enqueue(encoder.encode(
    'data: {"type":"token","text":"second delta"}\n\n'
    + 'data: {"type":"done"}\n\n',
  ));
  streamController.close();
  await streamed;

  olderResponse.resolve({
    messages: [{id: "older", role: "assistant", content: "old-page"}],
    messages_truncated: false,
    messages_oldest_cursor: null,
  });
  await olderRender;
  assert.deepEqual(messages.children, ["current-history", "live-user", assistantNode]);
  assert.deepEqual(assistantNode.children, ["first delta", "second delta"]);
  assert.deepEqual(
    state.messages.slice(-2).map((item) => item.content),
    ["live-user", "first deltasecond delta"],
  );
});

test("slower first workflow navigation cannot overwrite the newer card", async () => {
  for (const key of ["changeSet", "mapping"]) {
    const guard = guardApi.create();
    const card = fakeContainer();
    const first = deferred();
    const second = deferred();
    const firstToken = guardApi.begin(guard, key);
    const firstRender = first.promise.then((value) => {
      if (guardApi.current(guard, key, firstToken)) card.replaceChildren(value);
    });
    const secondToken = guardApi.begin(guard, key);
    const secondRender = second.promise.then((value) => {
      if (guardApi.current(guard, key, secondToken)) card.replaceChildren(value);
    });

    second.resolve("newer-selection");
    await secondRender;
    first.resolve("stale-selection");
    await firstRender;
    assert.deepEqual(card.children, ["newer-selection"]);
  }
});

test("new and refreshed workflow records remain navigable in their queues", () => {
  const oldRecord = {id: "old", status: "pending"};
  let queue = [oldRecord];
  queue = guardApi.upsertRecord(queue, {id: "new", status: "pending"});
  assert.deepEqual(queue.map((item) => item.id), ["new", "old"]);
  queue = guardApi.upsertRecord(queue, {id: "old", status: "applied"});
  assert.deepEqual(queue, [
    {id: "new", status: "pending"},
    {id: "old", status: "applied"},
  ]);
});
