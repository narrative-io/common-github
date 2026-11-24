// cache-uv's step: restores the project's archive. The action is JavaScript only so that save.js can run as its
// post step; ../s3-cache.js explains.
const { input, run } = require("../s3-cache.js");

process.exitCode = run("uv", "restore", input("working_directory"));
