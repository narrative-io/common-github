// cache-sbt's step: restores the archive. The action is JavaScript only so that save.js can run as its post step;
// ../s3-cache.js explains.
const { input, run } = require("../s3-cache.js");

process.exitCode = run("sbt", "restore", input("scope"));
