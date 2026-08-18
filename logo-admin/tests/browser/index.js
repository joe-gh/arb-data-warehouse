// Node's test runner treats a directory argument as its index module rather
// than discovering .mjs files on every supported Node line. Run the actual
// module in a child test process so its top-level tests retain their lifecycle.
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

require("node:test").test("browser regression modules", () => {
  const result = spawnSync(
    process.execPath,
    ["--test", path.join(__dirname, "test_agent_ui_races.mjs")],
    { encoding: "utf8" },
  );
  assert.equal(
    result.status,
    0,
    [result.stdout, result.stderr].filter(Boolean).join("\n"),
  );
});
