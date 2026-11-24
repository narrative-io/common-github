"""Restore or save a CI cache from S3. --kind picks what the archive holds: sbt is sbt's dependency caches (coursier,
ivy, the sbt launcher), mise is mise's data directory (the mise binary, and the tools and plugins it installed), uv is
uv's cache (the packages a project's `uv sync` downloaded).

The archive for a build lives at $S3_CACHE_URL/<repository>[/<scope>]/<key>.tar.zst for sbt, at
$S3_CACHE_URL/<repository>/mise-tools/<tools hash>/<key>.tar.zst for mise, and at
$S3_CACHE_URL/<repository>/uv/<project>/<key>.tar.zst for uv, where the key hashes the files the kind names, and
latest beside it names the key main saved last. A scope gives a set of jobs, such as one aggregate's, archives of
their own. Archives expire with the bucket's lifecycle rule; the first push to main after that saves the key again.
The callers are the per-tool actions beside this script: cache-sbt, setup-mise and cache-uv.

The runners start it with their own python3, so it uses only the standard library of the oldest Python they have,
3.11. What it decides is in pure functions; what it does to S3, tar, git and the clock goes through Effects, which
main builds and the tests replace.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Final, Protocol, TypeAlias

TIMEOUT_SECONDS: Final = 900

Env: TypeAlias = Mapping[str, str]


def no_details(home: Path) -> tuple[str, ...]:
    return ()


@dataclass(frozen=True)
class Contents:
    """What an archive holds, and which tracked files decide its key.

    Paths are relative to HOME, so an archive extracts in place on any runner. details lists what the log should
    show about the folders beyond their size, such as the tool versions installed.
    """

    paths: tuple[Path, ...]
    excludes: tuple[str, ...]
    is_key_file: Callable[[Path], bool]
    details: Callable[[Path], tuple[str, ...]] = no_details


# --- Each kind's key files and folders -------------------------------------------------------------------------------


def is_sbt_build_file(path: Path) -> bool:
    return (
        path.name.endswith(".sbt")
        or path == Path("mise.toml")
        or ("project" in path.parts[:-1] and (path.name.endswith(".scala") or path.name == "build.properties"))
    )


def is_mise_config_file(path: Path) -> bool:
    """Every mise config in the repository, not only the root's: a job that sets working_directory installs what its
    folder's config pins, on top of the root's."""
    return path.name in ("mise.toml", ".mise.toml", "mise.lock", ".tool-versions")


def mise_data_dir(env: Env, home: Path) -> Path:
    """Where mise keeps its binary, installs and plugins, resolved as jdx/mise-action resolves it when its mise_dir
    input is unset."""
    data_dir = env.get("MISE_DATA_DIR")
    xdg_data_home = env.get("XDG_DATA_HOME")
    return (
        Path(data_dir)
        if data_dir
        else Path(xdg_data_home) / "mise"
        if xdg_data_home
        else home / ".local" / "share" / "mise"
    )


def uv_cache_dir(env: Env, home: Path) -> Path:
    """Where uv keeps its cache, resolved as uv resolves it on Linux."""
    cache_dir = env.get("UV_CACHE_DIR")
    xdg_cache_home = env.get("XDG_CACHE_HOME")
    return Path(cache_dir) if cache_dir else Path(xdg_cache_home) / "uv" if xdg_cache_home else home / ".cache" / "uv"


def uv_project(env: Env) -> Path:
    """The project whose uv.lock keys the archive: the scope, else the repository root."""
    return Path(env.get("S3_CACHE_SCOPE", "").strip("/") or ".")


def sbt_dependencies(env: Env, home: Path) -> Contents:
    # Only caches: ~/.sbt/1.0 also holds global settings and plugins, which sbt compiles and runs in every build, so
    # an archive of all of ~/.sbt would carry code from the saving runner into every job that restores it.
    return Contents(
        paths=(Path(".cache/coursier"), Path(".ivy2/cache"), Path(".sbt/boot"), Path(".sbt/1.0/zinc")),
        excludes=("*.lock",),
        is_key_file=is_sbt_build_file,
    )


def mise_tools(env: Env, home: Path) -> Contents:
    # relative_to raises when MISE_DATA_DIR points outside HOME: an archive of an absolute path wouldn't extract in
    # place.
    data_dir = mise_data_dir(env, home).relative_to(home)
    # downloads/ holds the tarballs mise installed from, which are useless once installed and would double the
    # archive.
    return Contents(
        paths=(data_dir,),
        excludes=(str(data_dir / "downloads"), "*.lock"),
        is_key_file=is_mise_config_file,
        details=lambda root: installed_versions(root / data_dir / "installs"),
    )


def uv_packages(env: Env, home: Path) -> Contents:
    # relative_to raises when UV_CACHE_DIR points outside HOME, as for mise.
    cache_dir = uv_cache_dir(env, home).relative_to(home)
    lock = uv_project(env) / "uv.lock"
    # builds-v0/ holds the throwaway environments uv builds source distributions in.
    return Contents(
        paths=(cache_dir,),
        excludes=(str(cache_dir / "builds-v0"), "*.lock"),
        is_key_file=lambda path: path == lock,
    )


def installed_versions(installs: Path) -> tuple[str, ...]:
    """Each installed tool version, as "<tool> <version>". mise also links partial versions (java/21) to full ones;
    the links are left out, so a version a pin no longer names, and that prune should have deleted, stands out."""
    return tuple(
        sorted(
            f"{tool.name} {version.name}"
            for tool in installs.iterdir()
            if tool.is_dir()
            for version in tool.iterdir()
            if version.is_dir() and not version.is_symlink()
        )
        if installs.is_dir()
        else ()
    )


# --- The key ---------------------------------------------------------------------------------------------------------


def key_files(index: bytes, is_key_file: Callable[[Path], bool]) -> Iterator[tuple[Path, str]]:
    """The key files in `git ls-files --stage -z`'s output, each with the object ID of its content in the index.

    Taken from the index rather than the disk, so that the restore and the save of one job compute the same key: a
    job can add or edit build files between them, as sbt-dependency-submission does when it drops a plugin into
    project/.
    """
    # Each entry is "<mode> <object id> <stage>\t<path>".
    for entry in filter(None, index.split(b"\0")):
        meta, _, name = entry.partition(b"\t")
        path = Path(os.fsdecode(name))
        if is_key_file(path):
            yield path, meta.split()[1].decode()


def cache_key(index: bytes, contents: Contents) -> str:
    digest = hashlib.sha256()
    for path, object_id in sorted(key_files(index, contents.is_key_file)):
        digest.update(os.fsencode(path) + b"\0" + object_id.encode() + b"\0")
    return digest.hexdigest()[:32]


# --- Where an archive lives ------------------------------------------------------------------------------------------


def location(url: str, repository: str, kind: str, env: Env) -> str:
    """The S3 prefix an archive and its latest live under.

    mise archives go under a hash of the mise version, the directory mise runs in and the tools a job installs, so
    jobs that install different tools keep separate archives, and latest only falls back to an archive of the same
    tools. uv archives go under the project, so each project's packages keep an archive of their own.
    """
    match kind:
        case "mise":
            tools_hash = hashlib.sha256(env["S3_CACHE_TOOLS"].encode()).hexdigest()[:16]
            parts = [url, repository, "mise-tools", tools_hash]
        case "uv":
            parts = [url, repository, "uv", str(uv_project(env))]
        case _:
            parts = [url, repository, env.get("S3_CACHE_SCOPE", "")]
    return "/".join(part.strip("/") for part in parts if part.strip("/") and part.strip("/") != ".")


def archive_url(base: str, key: str) -> str:
    return f"{base}/{key}.tar.zst"


# --- Which archive a restore takes -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OnRunner:
    """The runner restored or saved this key last, so its folders already hold it."""

    key: str


@dataclass(frozen=True)
class InS3:
    key: str


@dataclass(frozen=True)
class Nowhere:
    pass


Source: TypeAlias = OnRunner | InS3 | Nowhere


def source(key: str, marked: str | None, latest: str | None, saved: Callable[[str], bool]) -> Source:
    """Where to restore from: the job's own key, else the key main saved last, each from the runner when its marker
    names it, else from S3 when saved there. saved is only asked about keys the runner doesn't have."""
    candidates = (key,) if latest in (None, "", key) else (key, latest)
    found = next((candidate for candidate in candidates if candidate == marked or saved(candidate)), None)
    return Nowhere() if found is None else OnRunner(found) if found == marked else InS3(found)


# --- What the log shows ----------------------------------------------------------------------------------------------


def human(size: int) -> str:
    return f"{size / 2**30:.2f} GiB" if size >= 2**30 else f"{size / 2**20:.1f} MiB"


@dataclass(frozen=True)
class FolderStats:
    path: Path
    size: int
    files: int


def is_excluded(path: Path, excludes: Sequence[str]) -> bool:
    """Whether tar leaves path, relative to HOME, out: a pattern with a slash names a path from HOME, any other
    pattern matches a file or folder name, as tar's --exclude does."""
    return any(path == Path(pattern) if "/" in pattern else fnmatch(path.name, pattern) for pattern in excludes)


def folder_stats(home: Path, path: Path, excludes: Sequence[str]) -> FolderStats:
    """The size and file count of what an archive of path holds. Links count as themselves, not as what they point
    to, as tar stores them."""
    size = 0
    files = 0
    for root, dirs, names in os.walk(home / path):
        relative = Path(root).relative_to(home)
        # os.walk descends into what's left in dirs, so excluded folders are skipped.
        dirs[:] = [d for d in dirs if not is_excluded(relative / d, excludes)]
        for name in names:
            if is_excluded(relative / name, excludes):
                continue
            try:
                size += os.lstat(Path(root) / name).st_size
                files += 1
            except OSError:
                pass
    return FolderStats(path, size, files)


def stats_lines(label: str, home: Path, contents: Contents) -> tuple[str, ...]:
    """What the cache folders hold after a restore or at a save, so runs can be compared: a line in the notices, and
    the breakdown in a collapsed group of the step's log."""
    stats = [folder_stats(home, path, contents.excludes) for path in contents.paths]
    size = sum(s.size for s in stats)
    files = sum(s.files for s in stats)
    return (
        f"::notice::{label}: on disk, {human(size)} in {files:,} files",
        f"::group::{label}: what's on disk",
        *(f"~/{s.path}: {human(s.size)} in {s.files:,} files" for s in stats),
        *contents.details(home),
        "::endgroup::",
    )


# --- The effects -----------------------------------------------------------------------------------------------------


class S3(Protocol):
    def exists(self, url: str) -> bool: ...

    def read_text(self, url: str) -> str | None:
        """The object's text, or None when it can't be read, as when there's none."""
        ...

    def download(self, url: str, path: Path) -> None: ...

    def upload(self, path: Path, url: str) -> None: ...

    def write_text(self, url: str, text: str) -> None: ...


class Archiver(Protocol):
    def pack(self, archive: Path, root: Path, paths: Sequence[Path], excludes: Sequence[str]) -> None: ...

    def unpack(self, archive: Path, root: Path) -> None: ...


@dataclass(frozen=True)
class Effects:
    s3: S3
    archiver: Archiver
    # `git ls-files --stage -z` in a workspace.
    index: Callable[[Path], bytes]
    clock: Callable[[], float]


def head_object_found(url: str, returncode: int, stderr: str) -> bool:
    """What `aws s3api head-object` says about an object: there, missing, or a failure to check."""
    if returncode != 0 and "Not Found" not in stderr and "404" not in stderr:
        raise RuntimeError(f"couldn't check {url}: {stderr.strip()}")
    return returncode == 0


class AwsCli:
    """S3 through the aws CLI the runners have."""

    def exists(self, url: str) -> bool:
        bucket, _, key = url.removeprefix("s3://").partition("/")
        result = subprocess.run(
            ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key],
            check=False,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        return head_object_found(url, result.returncode, result.stderr)

    def read_text(self, url: str) -> str | None:
        result = subprocess.run(
            ["aws", "s3", "cp", "--no-progress", url, "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        return result.stdout if result.returncode == 0 else None

    def download(self, url: str, path: Path) -> None:
        subprocess.run(["aws", "s3", "cp", "--no-progress", url, str(path)], check=True, timeout=TIMEOUT_SECONDS)

    def upload(self, path: Path, url: str) -> None:
        subprocess.run(["aws", "s3", "cp", "--no-progress", str(path), url], check=True, timeout=TIMEOUT_SECONDS)

    def write_text(self, url: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as file:
            file.write(text)
            file.flush()
            self.upload(Path(file.name), url)


def pack_command(archive: Path, root: Path, paths: Sequence[Path], excludes: Sequence[str]) -> list[str]:
    return [
        "tar",
        "-I",
        "zstd -T0 -3",
        "-cf",
        str(archive),
        *(f"--exclude={pattern}" for pattern in excludes),
        "-C",
        str(root),
        *map(str, paths),
    ]


def unpack_command(archive: Path, root: Path) -> list[str]:
    return ["tar", "-I", "zstd", "-xf", str(archive), "-C", str(root)]


class Tar:
    """zstd tar archives, through the runners' GNU tar and zstd."""

    def pack(self, archive: Path, root: Path, paths: Sequence[Path], excludes: Sequence[str]) -> None:
        subprocess.run(pack_command(archive, root, paths, excludes), check=True, timeout=TIMEOUT_SECONDS)

    def unpack(self, archive: Path, root: Path) -> None:
        subprocess.run(unpack_command(archive, root), check=True, timeout=TIMEOUT_SECONDS)


def git_index(workspace: Path) -> bytes:
    listed = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--stage", "-z"],
        check=False,
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {listed.stderr.decode(errors='replace').strip()}")
    return listed.stdout


# --- Restore and save ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cache:
    """One archive location, the key the job wants there, and the folders under home it holds."""

    home: Path
    base: str
    key: str
    contents: Contents
    # Where the archive is packed or downloaded on the runner.
    archive: Path


def markers(home: Path) -> Path:
    """One file per archive location, naming the key this runner last restored or saved for it. Runners take several
    jobs before they are replaced, so the next job of the same scope on the same runner usually needs no download."""
    return home / ".cache" / "sbt-dependency-cache"


def marker(home: Path, base: str) -> Path:
    return markers(home) / hashlib.sha256(base.encode()).hexdigest()[:16]


def marked_key(home: Path, base: str) -> str | None:
    try:
        return marker(home, base).read_text().strip()
    except FileNotFoundError:
        return None


def mark(home: Path, base: str, key: str) -> None:
    markers(home).mkdir(parents=True, exist_ok=True)
    marker(home, base).write_text(key)


def discard(home: Path, contents: Contents) -> None:
    # Several locations can extract into the same folders (every sbt scope shares the coursier cache), so a broken
    # extraction invalidates every marker.
    shutil.rmtree(markers(home), ignore_errors=True)
    for path in contents.paths:
        shutil.rmtree(home / path, ignore_errors=True)


def download(cache: Cache, effects: Effects, key: str) -> str:
    started = effects.clock()
    effects.s3.download(archive_url(cache.base, key), cache.archive)
    downloaded = effects.clock()
    # Drop the marker first, so a failed extraction can't leave a runner claiming a key it doesn't have.
    marker(cache.home, cache.base).unlink(missing_ok=True)
    try:
        effects.archiver.unpack(cache.archive, cache.home)
    except Exception:
        # A half-extracted jar looks present to coursier, and a half-extracted JDK looks installed to mise, so start
        # from nothing rather than from that.
        discard(cache.home, cache.contents)
        raise
    mark(cache.home, cache.base, key)
    wanted = "" if key == cache.key else f" (newest from main; wanted {cache.key})"
    return (
        f"restored {key}{wanted}: {human(cache.archive.stat().st_size)} downloaded in {downloaded - started:.0f}s,"
        f" unpacked in {effects.clock() - downloaded:.0f}s"
    )


def restore(cache: Cache, effects: Effects) -> str:
    marked = marked_key(cache.home, cache.base)
    latest = None if marked == cache.key else effects.s3.read_text(f"{cache.base}/latest")
    found = source(
        cache.key, marked, latest and latest.strip(), lambda key: effects.s3.exists(archive_url(cache.base, key))
    )
    match found:
        case OnRunner(key) if key == cache.key:
            return f"runner already has {key}"
        case OnRunner(key):
            return f"runner already has {key}, the newest from main"
        case InS3(key):
            return download(cache, effects, key)
        case Nowhere():
            return f"no archive for {cache.key} or from main yet, so everything is downloaded"


def save(cache: Cache, effects: Effects) -> str:
    url = archive_url(cache.base, cache.key)
    if effects.s3.exists(url):
        mark(cache.home, cache.base, cache.key)
        outcome = f"{cache.key} is already saved"
    else:
        paths = [path for path in cache.contents.paths if (cache.home / path).is_dir()]
        if not paths:
            raise RuntimeError(f"none of {', '.join(map(str, cache.contents.paths))} exist under {cache.home}")
        started = effects.clock()
        effects.archiver.pack(cache.archive, cache.home, paths, cache.contents.excludes)
        packed = effects.clock()
        effects.s3.upload(cache.archive, url)
        uploaded = effects.clock()
        # Only after an upload: an older commit's job that finishes last must not point latest back at its key.
        effects.s3.write_text(f"{cache.base}/latest", cache.key)
        mark(cache.home, cache.base, cache.key)
        outcome = (
            f"saved {cache.key}: {human(cache.archive.stat().st_size)} packed in {packed - started:.0f}s,"
            f" uploaded in {uploaded - packed:.0f}s"
        )
    return outcome


# --- The kinds, and the command line ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    """One --kind: how to build its Contents, and how its notices read."""

    contents: Callable[[Env, Path], Contents]
    label: str
    without_it: str


KINDS: Final = {
    "sbt": Kind(sbt_dependencies, "sbt dependency cache", "sbt resolves everything"),
    "mise": Kind(mise_tools, "mise tools cache", "mise downloads its tools"),
    "uv": Kind(uv_packages, "uv cache", "uv downloads every package"),
}


def run(argv: Sequence[str], env: Env, home: Path, effects: Effects, out: Callable[[str], None]) -> int:
    """Restores or saves, writing its notices and warnings to out, and returns the exit status: 1 only without a URL,
    since any other failure only costs the job its cache."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["save", "restore"])
    parser.add_argument("--kind", choices=KINDS, required=True)
    args = parser.parse_args(argv)
    kind = KINDS[args.kind]
    url = env.get("S3_CACHE_URL", "").rstrip("/")
    if not url:
        # Callers pass it from an organization variable, which a repository may not have. Failing the step, unlike
        # the warnings below, keeps a job from running without its cache unnoticed.
        out(
            f"::error::{kind.label}: no S3 URL. Pass the action's url input, such as the organization variable"
            " COMMON_GITHUB_BUILD_DEPENDENCY_CACHE."
        )
        return 1
    archive = Path(env["RUNNER_TEMP"], f"s3-cache-{args.kind}.tar.zst")
    start = effects.clock()
    try:
        # Built inside the try: mise_tools raises when mise's data directory is outside HOME, and that only costs the
        # job its cache.
        contents = kind.contents(env, home)
        cache = Cache(
            home=home,
            base=location(url, env["GITHUB_REPOSITORY"].split("/")[-1], args.kind, env),
            key=cache_key(effects.index(Path(env["GITHUB_WORKSPACE"])), contents),
            contents=contents,
            archive=archive,
        )
        outcome = save(cache, effects) if args.mode == "save" else restore(cache, effects)
        out(f"::notice::{kind.label}: {outcome} ({effects.clock() - start:.0f}s in all)")
        # Its own try: stats that fail to compute cost the log a line, not the cache.
        try:
            for line in stats_lines(kind.label, home, contents):
                out(line)
        except Exception as e:  # noqa: BLE001 -- any failure here costs the log a line, and no more
            out(f"::warning::{kind.label}: couldn't measure what's on disk: {e}")
    except Exception as e:  # noqa: BLE001 -- any failure costs the job its cache, not the job itself
        out(
            f"::warning::{kind.label}: couldn't restore, so {kind.without_it}: {e}"
            if args.mode == "restore"
            else f"::warning::{kind.label}: couldn't save, so other runs restore an older one: {e}"
        )
    finally:
        archive.unlink(missing_ok=True)
    return 0


def main() -> None:
    effects = Effects(s3=AwsCli(), archiver=Tar(), index=git_index, clock=time.monotonic)
    raise SystemExit(run(sys.argv[1:], os.environ, Path.home(), effects, print))


if __name__ == "__main__":
    main()
