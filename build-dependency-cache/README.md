# build-dependency-cache

Actions that keep CI's dependency and tool caches in S3 instead of GitHub's cache. Our self-hosted runners sit in a
private subnet: they reach GitHub's cache, which is Azure storage, and package registries through a NAT gateway that
bills every byte, and S3 in their own region through a gateway endpoint that's free. Moving sbt's caches to S3 removed
most of the NAT's traffic (SC-87124); mise's tools were most of what was left (SC-88184).

- [`cache-sbt`](cache-sbt/action.yml): sbt's dependency caches (coursier, ivy, the sbt launcher).
- [`setup-mise`](setup-mise/action.yml): `jdx/mise-action`, with mise and the tools it installs kept in S3.
- [`cache-uv`](cache-uv/action.yml): uv's cache, the packages a project's `uv sync` downloads.
- [`s3_cache.py`](s3_cache.py): the archive logic all three share, and [`s3-cache.js`](s3-cache.js), which the
  JavaScript actions start it with. Callers never run either directly.

## Configure

The actions need the base S3 URL of the cache, `s3://<bucket>/<prefix>`, as their `url` input. It's kept out of this
public repository, in an organization variable:

1. In the organization's settings, under **Secrets and variables → Actions → Variables**, create the variable
   `COMMON_GITHUB_BUILD_DEPENDENCY_CACHE` with that URL.
2. Under **Repository access**, give it to every repository that uses these actions. A repository the variable isn't
   shared with gets an empty `url`, and each cache step fails, so no job runs without its cache unnoticed. Make sure
   it's a variable, not a secret: the steps read `vars`, and a secret of the same name leaves `url` empty.
3. Give the runners read and write on that prefix, and an expiry rule for it.

An action can't read the variable itself (its `action.yml` can't refer to `vars`), so every step passes it, as below.
The bucket, its expiry rule and the runners' access to it are in narrative-devtools (`var/shared/` and
`github-runner/terraform/iam.tf`).

## Use

Pin a released version by its full commit SHA, as for everything in this repository:

```yaml
- name: Restore the sbt dependency cache
  # Repository variable: "off" turns the cache off.
  if: vars.SBT_DEPENDENCY_CACHE != 'off'
  uses: narrative-io/common-github/build-dependency-cache/cache-sbt@<full-40-char-sha> # vX.Y.Z
  with:
    url: ${{ vars.COMMON_GITHUB_BUILD_DEPENDENCY_CACHE }}
    save: true # only in the job whose dependencies the archive should hold, such as sbt test

- name: Set up mise
  uses: narrative-io/common-github/build-dependency-cache/setup-mise@<full-40-char-sha> # vX.Y.Z
  with:
    url: ${{ vars.COMMON_GITHUB_BUILD_DEPENDENCY_CACHE }}
    version: 2026.11.10
    install_args: java asdf:mise-plugins/mise-sbt
    s3_cache: ${{ vars.MISE_TOOLS_CACHE != 'off' }}

- name: Restore the uv cache
  if: vars.UV_CACHE != 'off'
  uses: narrative-io/common-github/build-dependency-cache/cache-uv@<full-40-char-sha> # vX.Y.Z
  with:
    url: ${{ vars.COMMON_GITHUB_BUILD_DEPENDENCY_CACHE }}
    working_directory: my-project # the folder holding its uv.lock
    save: true # only in the job that syncs everything the project locks, such as its tests
```

## How an archive works

Each call archives one kind of cache, as `zstd`-compressed tar, at `<url>/<repository>/<prefix>/<key>.tar.zst`, with a
`latest` file beside it naming the key `main` saved last.

|                                    | sbt (`cache-sbt`)                                                             | mise (`setup-mise`)                                                         | uv (`cache-uv`)                                       |
| ---------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------- |
| Folders, under `HOME`              | `.cache/coursier`, `.ivy2/cache`, `.sbt/boot`, `.sbt/1.0/zinc`                | `.local/share/mise`, without `downloads/`                                   | `.cache/uv` (or `UV_CACHE_DIR`), without `builds-v0/` |
| Prefix                             | the optional `scope` input                                                    | `mise-tools/<hash of the mise version, working_directory and install_args>` | `uv/<working_directory>`                              |
| Key: a hash of these tracked files | every `*.sbt`; `*.scala` and `build.properties` under `project/`; `mise.toml` | every `mise.toml`, `.mise.toml`, `mise.lock` and `.tool-versions`           | the project's `uv.lock`                               |
| Saves                              | from its post step, when the job ends with every step passed, if `save: true` | right after `mise-action` installs, after `mise prune --yes --tools`        | from its post step, as `cache-sbt`, if `save: true`   |

- **The key** hashes each key file's object ID in git's index, not the file on disk, so a job that edits build files
  between restore and save, as `sbt-dependency-submission` does in `project/`, still saves under the key it restored.
- **Restore** takes the archive for the exact key, else the one `latest` names, and the tool fetches whatever changed
  since. A runner that already restored or saved the key skips the download: runners take several jobs before they're
  replaced. A half-extracted archive is deleted, folders and all, rather than left looking complete.
- **Save** only acts on a push to `main`, and only for a key that has no archive yet; `latest` moves only after the
  upload. Archives expire with the bucket's lifecycle rule, and the next push to `main` saves the key again.
- **Failures only warn, except a missing URL.** A restore or save that fails, because S3 can't be reached or an archive
  is broken, logs a warning and the job carries on; the tool then downloads what it needs. An empty `url`, or a
  JavaScript action that can't start Python, fails the step instead, since every job would otherwise quietly run
  without its cache.
- **Each restore and save reports** a notice with the archive's compressed size and how long the transfer and the tar
  took, then what the folders hold on disk. The step's log breaks it down per folder in a collapsed group and, for
  mise, lists the installed versions. Compare `main`'s saves over time to see whether an archive grows.

### Why `cache-sbt` and `cache-uv` are JavaScript and `setup-mise` is composite

`cache-sbt` and `cache-uv` are JavaScript actions only to get a post step, which saves when the job ends, after the
build or `uv sync` has fetched everything, as `actions/cache` does. Composite actions can't have one, and without it
every workflow would need a second step, placed last, to save. Their scripts only start `s3_cache.py`, through
`s3-cache.js`.

`setup-mise` has to be composite to call `jdx/mise-action`, and saves right after the install instead: mise's folder is
complete then, a job that fails later still saves it, and later steps can't add to it. Saving at the end would let the
job's real work start a few seconds sooner on the pushes that save; its save step's comment explains the trade-off.

None calls a sibling action: an action here can only reach another one with
`uses: narrative-io/common-github/<path>@<commit>`, and a commit can't name itself. They run `s3_cache.py` from the
checkout of this repository the runner made for the action.

### What's trusted

`mise-action` checks the restored mise binary against mise's signed checksums before running it. Nothing checks the
tools, the jars or the packages. Only a push to `main` saves, but that's the actions' own check, not a permission:
every job on the runner fleet can write the prefix, so an archive is only as trusted as everything that runs on the
runners, pull requests included. GitHub's cache, by contrast, is scoped by branch.

## Test

The folder is a uv project, set up with mise: its `mise.toml` pins Python 3.11, the oldest a runner has, and the
repository's pins uv and node. From the folder:

```sh
uv run pytest                        # s3_cache.py
node --test tests/s3-cache.test.js   # s3-cache.js, which cache-sbt and cache-uv run
uv run ruff check . && uv run ruff format --check . && uv run ty check
```

`s3_cache.py` keeps what it decides in pure functions, which the tests check with plain values, and takes S3, tar, git
and the clock as parameters, so its restores and saves are tested against an S3 kept in memory and a throwaway `HOME`.
The actions still run it with the runner's own `python3`: it needs nothing beyond the standard library. One test packs
and unpacks with the real tar and zstd, and skips where tar isn't GNU tar, as on macOS. The `build-dependency-cache`
workflow runs all of it on every pull request that changes this folder.
