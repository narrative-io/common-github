// Tests ../s3-cache.js, which the JavaScript actions run, with fakes for spawnSync and console.log. Run with
// `node --test tests/s3-cache.test.js` from the folder above.
const assert = require("node:assert/strict");
const { test } = require("node:test");

const { input, shouldSave, run, saveIfAsked } = require("../s3-cache.js");

const MAIN = { GITHUB_EVENT_NAME: "push", GITHUB_REF: "refs/heads/main" };

// Records each spawn, and answers with result.
function fakeSpawn(result) {
  const calls = [];
  const spawn = (command, args, options) => {
    calls.push({ command, args, env: options.env });
    return result;
  };
  return { calls, spawn };
}

test("input reads INPUT_<NAME>, trimmed, and is empty when unset", () => {
  assert.equal(input("working_directory", { INPUT_WORKING_DIRECTORY: " sub \n" }), "sub");
  assert.equal(input("scope", {}), "");
});

test("saves only with save: true on a push to main", () => {
  assert.equal(shouldSave({ ...MAIN, INPUT_SAVE: "true" }), true);
  assert.equal(shouldSave({ ...MAIN, INPUT_SAVE: "false" }), false);
  assert.equal(
    shouldSave({ INPUT_SAVE: "true", GITHUB_EVENT_NAME: "pull_request", GITHUB_REF: "refs/pull/1/merge" }),
    false,
  );
  assert.equal(
    shouldSave({ INPUT_SAVE: "true", GITHUB_EVENT_NAME: "push", GITHUB_REF: "refs/heads/feature" }),
    false,
  );
});

test("run passes the kind, mode, url and scope to s3_cache.py", () => {
  const { calls, spawn } = fakeSpawn({ status: 0 });
  const env = { INPUT_URL: "s3://bucket/prefix", PATH: "/bin" };
  assert.equal(run("uv", "restore", "sub", { env, spawn, log: () => {} }), 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, "python3");
  assert.match(calls[0].args[0], /s3_cache\.py$/);
  assert.deepEqual(calls[0].args.slice(1), ["restore", "--kind", "uv"]);
  assert.equal(calls[0].env.S3_CACHE_URL, "s3://bucket/prefix");
  assert.equal(calls[0].env.S3_CACHE_SCOPE, "sub");
  assert.equal(calls[0].env.PATH, "/bin");
});

test("run fails the step, and says why, when s3_cache.py fails", () => {
  const lines = [];
  const { spawn } = fakeSpawn({ status: 1 });
  assert.equal(run("sbt", "restore", "", { env: {}, spawn, log: (line) => lines.push(line) }), 1);
  assert.match(lines[0], /^::error::sbt cache: couldn't restore, .*s3_cache\.py failed: exit status 1$/);
});

test("run fails the step, and says why, when python3 can't start", () => {
  const lines = [];
  const { spawn } = fakeSpawn({ status: null, error: new Error("spawnSync python3 ENOENT") });
  assert.equal(run("sbt", "restore", "", { env: {}, spawn, log: (line) => lines.push(line) }), 1);
  assert.match(lines[0], /failed: spawnSync python3 ENOENT$/);
});

test("saveIfAsked runs a save only when asked, on main", () => {
  const skipped = fakeSpawn({ status: 0 });
  assert.equal(saveIfAsked("sbt", "", { env: { INPUT_SAVE: "true" }, spawn: skipped.spawn }), 0);
  assert.equal(skipped.calls.length, 0);

  const saved = fakeSpawn({ status: 0 });
  assert.equal(saveIfAsked("sbt", "agg", { env: { ...MAIN, INPUT_SAVE: "true" }, spawn: saved.spawn }), 0);
  assert.deepEqual(saved.calls[0].args.slice(1), ["save", "--kind", "sbt"]);
  assert.equal(saved.calls[0].env.S3_CACHE_SCOPE, "agg");
});
