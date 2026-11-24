// cache-sbt's post step, the reason the action is JavaScript (../s3-cache.js explains): it runs when the job ends,
// and only if every step passed (post-if: success() in action.yml).
const { input, saveIfAsked } = require("../s3-cache.js");

process.exitCode = saveIfAsked("sbt", input("scope"));
