// What the JavaScript actions in this folder run. They're JavaScript actions for one reason: to get a post step
// (their save.js), which saves when the job ends, as actions/cache does. Composite actions can't have one, and
// without it every workflow would need a second step, placed last, to save. So the JavaScript only starts
// s3_cache.py, beside this file, where the archive logic lives. The script logs its own notices and warnings, and
// only fails when it has no URL; the step fails with it, or when Python can't start, so a job can't run without
// its cache unnoticed.
//
// The process's environment, spawnSync and console.log are parameters, so tests/s3-cache.test.js can pass fakes.
const { spawnSync } = require("node:child_process");
const { appendFileSync } = require("node:fs");
const path = require("node:path");

const SCRIPT = path.join(__dirname, "s3_cache.py");

// The runner passes each input as INPUT_<NAME> to both the main and the post step, defaults included.
function input(name, env = process.env) {
  return (env[`INPUT_${name.toUpperCase()}`] || "").trim();
}

// Saves only in a job that passes save as true, and only on a push to main. It's this check that keeps pull
// requests from saving, not IAM, since every job on the runner fleet can write the prefix.
function shouldSave(env = process.env) {
  return (
    input("save", env) === "true" &&
    env.GITHUB_EVENT_NAME === "push" &&
    env.GITHUB_REF === "refs/heads/main"
  );
}

// The variables the aws CLI reads its region from. configure-aws-credentials' post step sets both to "" when the job
// ends, and post steps run in the reverse order of their steps, so a cache step placed before it saves after that.
// GitHub Actions can't unset a variable, and the aws CLI takes "" as the region: it calls https://s3..amazonaws.com.
// The credentials it blanks too are fine: the CLI skips empty ones and falls back to the runner's instance role,
// the one the restore used before configure-aws-credentials ran.
const REGION_VARIABLES = ["AWS_REGION", "AWS_DEFAULT_REGION"];

// Keeps the region the restore ran with in the step's state, which the runner hands to the post step as
// STATE_<name>. Failing to only means a save may run without a region, so it warns.
function rememberRegion(env, append, log) {
  const lines = REGION_VARIABLES.filter((name) => env[name]).map((name) => `${name}=${env[name]}\n`);
  if (!env.GITHUB_STATE || lines.length === 0) return;
  try {
    append(env.GITHUB_STATE, lines.join(""));
  } catch (error) {
    log(`::warning::couldn't keep the AWS region for the save: ${error.message}`);
  }
}

// env, with each region variable that's now empty set back to the value the restore ran with.
function withRestoredRegion(env) {
  const restored = REGION_VARIABLES.filter((name) => !env[name] && env[`STATE_${name}`]).map((name) => [
    name,
    env[`STATE_${name}`],
  ]);
  return { ...env, ...Object.fromEntries(restored) };
}

// Runs s3_cache.py and returns the step's exit status. scope is the folder that keeps the job's archives apart: an
// aggregate for sbt, the project for uv.
function run(
  kind,
  mode,
  scope,
  { env = process.env, spawn = spawnSync, log = console.log, append = appendFileSync } = {},
) {
  if (mode === "restore") rememberRegion(env, append, log);
  const scriptEnv = mode === "save" ? withRestoredRegion(env) : env;
  const result = spawn("python3", [SCRIPT, mode, "--kind", kind], {
    stdio: "inherit",
    env: { ...scriptEnv, S3_CACHE_URL: input("url", env), S3_CACHE_SCOPE: scope },
  });
  const failed = Boolean(result.error) || result.status !== 0;
  if (failed) {
    const why = result.error
      ? result.error.message
      : `exit status ${result.status}`;
    log(`::error::${kind} cache: couldn't ${mode}, ${SCRIPT} failed: ${why}`);
  }
  return failed ? 1 : 0;
}

function saveIfAsked(kind, scope, effects = {}) {
  return shouldSave(effects.env) ? run(kind, "save", scope, effects) : 0;
}

module.exports = { input, shouldSave, run, saveIfAsked };
