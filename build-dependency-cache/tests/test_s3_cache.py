import shutil
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import s3_cache
from s3_cache import Effects, InS3, Nowhere, OnRunner

# --- Decisions -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("build.sbt", True),
        ("aggregate/module.sbt", True),
        ("mise.toml", True),
        ("project/Dependencies.scala", True),
        ("project/build.properties", True),
        ("nql/project/plugins.scala", True),
        ("sub/mise.toml", False),
        ("src/main/scala/Main.scala", False),
        ("build.properties", False),
    ],
)
def test_sbt_build_files(path: str, *, expected: bool) -> None:
    assert s3_cache.is_sbt_build_file(Path(path)) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("mise.toml", True),
        ("sub/.mise.toml", True),
        ("sub/mise.lock", True),
        (".tool-versions", True),
        ("mise.toml.bak", False),
    ],
)
def test_mise_config_files(path: str, *, expected: bool) -> None:
    assert s3_cache.is_mise_config_file(Path(path)) is expected


HOME = Path("/home/runner")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MISE_DATA_DIR": "/opt/mise", "XDG_DATA_HOME": "/xdg"}, "/opt/mise"),
        ({"XDG_DATA_HOME": "/xdg"}, "/xdg/mise"),
        ({}, "/home/runner/.local/share/mise"),
    ],
)
def test_mise_data_dir(env: Mapping[str, str], expected: str) -> None:
    assert s3_cache.mise_data_dir(env, HOME) == Path(expected)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"UV_CACHE_DIR": "/opt/uv", "XDG_CACHE_HOME": "/xdg"}, "/opt/uv"),
        ({"XDG_CACHE_HOME": "/xdg"}, "/xdg/uv"),
        ({}, "/home/runner/.cache/uv"),
    ],
)
def test_uv_cache_dir(env: Mapping[str, str], expected: str) -> None:
    assert s3_cache.uv_cache_dir(env, HOME) == Path(expected)


def test_mise_tools_leave_out_downloads() -> None:
    contents = s3_cache.mise_tools({}, HOME)
    assert contents.paths == (Path(".local/share/mise"),)
    assert ".local/share/mise/downloads" in contents.excludes


def test_mise_tools_refuse_a_data_directory_outside_home() -> None:
    with pytest.raises(ValueError, match="mise"):
        s3_cache.mise_tools({"MISE_DATA_DIR": "/opt/mise"}, HOME)


def test_uv_packages_are_keyed_by_their_project_lock_and_leave_out_builds() -> None:
    contents = s3_cache.uv_packages({"S3_CACHE_SCOPE": "/sub/"}, HOME)
    assert contents.is_key_file(Path("sub/uv.lock"))
    assert not contents.is_key_file(Path("uv.lock"))
    assert ".cache/uv/builds-v0" in contents.excludes


def test_uv_packages_default_to_the_root_project() -> None:
    assert s3_cache.uv_packages({}, HOME).is_key_file(Path("uv.lock"))


def index(entries: Mapping[str, str]) -> bytes:
    """What `git ls-files --stage -z` prints for these paths and object IDs."""
    return b"".join(f"100644 {object_id} 0\t{path}\0".encode() for path, object_id in entries.items())


def test_key_files_are_the_matching_paths_with_their_object_ids() -> None:
    listed = index({"build.sbt": "a1", "README.md": "b2", "project/build.properties": "c3"})
    assert list(s3_cache.key_files(listed, s3_cache.is_sbt_build_file)) == [
        (Path("build.sbt"), "a1"),
        (Path("project/build.properties"), "c3"),
    ]


def test_the_key_ignores_order_and_other_files_but_not_content() -> None:
    sbt = s3_cache.sbt_dependencies({}, HOME)
    key = s3_cache.cache_key(index({"build.sbt": "a1", "project/build.properties": "c3"}), sbt)
    assert s3_cache.cache_key(index({"project/build.properties": "c3", "build.sbt": "a1", "x.md": "d4"}), sbt) == key
    assert s3_cache.cache_key(index({"build.sbt": "a2", "project/build.properties": "c3"}), sbt) != key
    assert len(key) == 32


def test_sbt_archives_live_under_the_repository_and_scope() -> None:
    assert s3_cache.location("s3://b/p", "repo", "sbt", {}) == "s3://b/p/repo"
    assert s3_cache.location("s3://b/p", "repo", "sbt", {"S3_CACHE_SCOPE": "/agg/"}) == "s3://b/p/repo/agg"


def test_mise_archives_live_under_a_hash_of_the_tools() -> None:
    java = s3_cache.location("s3://b/p", "repo", "mise", {"S3_CACHE_TOOLS": "2026.11.10  java"})
    python = s3_cache.location("s3://b/p", "repo", "mise", {"S3_CACHE_TOOLS": "2026.11.10 sub python uv"})
    assert java.startswith("s3://b/p/repo/mise-tools/")
    assert len(java.rsplit("/", 1)[1]) == 16
    assert java != python


def test_uv_archives_live_under_the_project() -> None:
    assert s3_cache.location("s3://b/p", "repo", "uv", {"S3_CACHE_SCOPE": "sub"}) == "s3://b/p/repo/uv/sub"
    assert s3_cache.location("s3://b/p", "repo", "uv", {}) == "s3://b/p/repo/uv"


def never(key: str) -> bool:
    raise AssertionError(f"asked S3 about {key}")


@pytest.mark.parametrize(
    ("marked", "latest", "saved", "expected"),
    [
        ("k", None, (), OnRunner("k")),
        (None, None, ("k",), InS3("k")),
        (None, "old", ("k", "old"), InS3("k")),
        (None, "old", ("old",), InS3("old")),
        ("old", "old", (), OnRunner("old")),
        (None, "k", (), Nowhere()),
        (None, "", (), Nowhere()),
        (None, None, (), Nowhere()),
    ],
)
def test_where_a_restore_comes_from(
    marked: str | None, latest: str | None, saved: tuple[str, ...], expected: s3_cache.Source
) -> None:
    assert s3_cache.source("k", marked, latest, lambda key: key in saved) == expected


def test_a_key_the_runner_has_asks_nothing_of_s3() -> None:
    assert s3_cache.source("k", "k", "old", never) == OnRunner("k")


def test_head_object_found() -> None:
    assert s3_cache.head_object_found("s3://b/k", 0, "")
    assert not s3_cache.head_object_found("s3://b/k", 254, "An error occurred (404) ...: Not Found")
    with pytest.raises(RuntimeError, match="couldn't check s3://b/k: Access Denied"):
        s3_cache.head_object_found("s3://b/k", 254, "Access Denied\n")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".cache/uv/builds-v0", True),
        (".cache/uv/wheels-v5/builds-v0", False),
        (".ivy2/cache/x.lock", True),
        (".ivy2/cache/x.jar", False),
    ],
)
def test_is_excluded(path: str, *, expected: bool) -> None:
    assert s3_cache.is_excluded(Path(path), (".cache/uv/builds-v0", "*.lock")) is expected


def test_human_sizes() -> None:
    assert s3_cache.human(3 * 2**20) == "3.0 MiB"
    assert s3_cache.human(3 * 2**30) == "3.00 GiB"


def test_tar_commands() -> None:
    archive = Path("/tmp/a.tar.zst")
    assert s3_cache.pack_command(archive, HOME, [Path(".ivy2/cache")], ["*.lock"]) == [
        *("tar", "-I", "zstd -T0 -3", "-cf", "/tmp/a.tar.zst", "--exclude=*.lock"),
        *("-C", "/home/runner", ".ivy2/cache"),
    ]
    assert s3_cache.unpack_command(archive, HOME) == [
        "tar",
        "-I",
        "zstd",
        "-xf",
        "/tmp/a.tar.zst",
        "-C",
        "/home/runner",
    ]


# --- Disk ------------------------------------------------------------------------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_installed_versions_leave_out_alias_links(tmp_path: Path) -> None:
    (tmp_path / "java" / "21.0.13").mkdir(parents=True)
    (tmp_path / "java" / "21").symlink_to("21.0.13")
    (tmp_path / "uv" / "0.13.7").mkdir(parents=True)
    assert s3_cache.installed_versions(tmp_path) == ("java 21.0.13", "uv 0.13.7")
    assert s3_cache.installed_versions(tmp_path / "missing") == ()


def test_folder_stats_leave_out_excluded_files_and_folders(tmp_path: Path) -> None:
    write(tmp_path / ".cache/uv/wheels-v5/a.whl", "12345")
    write(tmp_path / ".cache/uv/x.lock", "1")
    write(tmp_path / ".cache/uv/builds-v0/env", "1")
    stats = s3_cache.folder_stats(tmp_path, Path(".cache/uv"), (".cache/uv/builds-v0", "*.lock"))
    assert (stats.size, stats.files) == (5, 1)


def test_git_index_lists_what_git_tracks(tmp_path: Path) -> None:
    write(tmp_path / "build.sbt", "lazy val root = project\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "build.sbt"], check=True)
    assert [path for path, _ in s3_cache.key_files(s3_cache.git_index(tmp_path), s3_cache.is_sbt_build_file)] == [
        Path("build.sbt")
    ]


def test_git_index_fails_outside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git ls-files failed"):
        s3_cache.git_index(tmp_path)


@pytest.mark.skipif(
    shutil.which("zstd") is None
    or "GNU tar" not in subprocess.run(["tar", "--version"], capture_output=True, text=True, check=False).stdout,
    reason="needs GNU tar and zstd, as the runners have; macOS's tar reads -I as --files-from",
)
def test_tar_round_trip(tmp_path: Path) -> None:
    write(tmp_path / "home/.ivy2/cache/a.jar", "jar")
    write(tmp_path / "home/.ivy2/cache/a.lock", "lock")
    archive = tmp_path / "a.tar.zst"
    s3_cache.Tar().pack(archive, tmp_path / "home", [Path(".ivy2/cache")], ["*.lock"])
    (tmp_path / "restored").mkdir()
    s3_cache.Tar().unpack(archive, tmp_path / "restored")
    assert (tmp_path / "restored/.ivy2/cache/a.jar").read_text() == "jar"
    assert not (tmp_path / "restored/.ivy2/cache/a.lock").exists()


# --- Restore and save, against an S3 in memory -----------------------------------------------------------------------


@dataclass
class FakeS3:
    objects: dict[str, bytes] = field(default_factory=dict)

    def exists(self, url: str) -> bool:
        return url in self.objects

    def read_text(self, url: str) -> str | None:
        return self.objects[url].decode() if url in self.objects else None

    def download(self, url: str, path: Path) -> None:
        path.write_bytes(self.objects[url])

    def upload(self, path: Path, url: str) -> None:
        self.objects[url] = path.read_bytes()

    def write_text(self, url: str, text: str) -> None:
        self.objects[url] = text.encode()

    def keys(self, suffix: str) -> list[str]:
        return sorted(url for url in self.objects if url.endswith(suffix))


class PlainTar:
    """An uncompressed tar through tarfile, leaving out what tar's --exclude would: the runners' GNU tar and zstd
    aren't on every laptop."""

    def pack(self, archive: Path, root: Path, paths: Sequence[Path], excludes: Sequence[str]) -> None:
        with tarfile.open(archive, "w") as tar:
            for path in paths:
                tar.add(
                    root / path,
                    arcname=str(path),
                    filter=lambda info: None if s3_cache.is_excluded(Path(info.name), excludes) else info,
                )

    def unpack(self, archive: Path, root: Path) -> None:
        with tarfile.open(archive) as tar:
            tar.extractall(root, filter="tar")


class BrokenTar(PlainTar):
    def unpack(self, archive: Path, root: Path) -> None:
        write(root / ".cache/coursier/half.jar", "half")
        raise OSError("disk full")


@dataclass
class Runner:
    """One runner's HOME and workspace, and what it runs s3_cache.py against."""

    root: Path
    s3: FakeS3 = field(default_factory=FakeS3)
    archiver: s3_cache.Archiver = field(default_factory=PlainTar)
    tracked: dict[str, str] = field(default_factory=lambda: {"build.sbt": "a1", "mise.toml": "b2"})

    @property
    def home(self) -> Path:
        return self.root / "home"

    def run(self, *argv: str, **env: str) -> tuple[int, list[str]]:
        lines: list[str] = []
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        effects = Effects(s3=self.s3, archiver=self.archiver, index=lambda _: index(self.tracked), clock=lambda: 0.0)
        status = s3_cache.run(
            argv,
            {
                "S3_CACHE_URL": "s3://bucket/prefix/",
                "RUNNER_TEMP": str(self.root / "tmp"),
                "GITHUB_REPOSITORY": "narrative-io/example",
                "GITHUB_WORKSPACE": str(self.root / "ws"),
                **env,
            },
            self.home,
            effects,
            lines.append,
        )
        return status, lines

    def outcome(self, *argv: str, **env: str) -> str:
        status, lines = self.run(*argv, **env)
        assert status == 0
        return lines[0]

    def new_runner(self, name: str) -> "Runner":
        """Another runner of the fleet: same S3 and workspace, an empty HOME."""
        return Runner(self.root.parent / name, self.s3, self.archiver, self.tracked)


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path / "first")


def test_save_uploads_the_archive_and_latest(runner: Runner) -> None:
    write(runner.home / ".cache/coursier/v1/a.jar", "jar")
    status, lines = runner.run("save", "--kind", "sbt")
    assert status == 0
    assert lines[0].startswith("::notice::sbt dependency cache: saved ")
    assert lines[1] == "::notice::sbt dependency cache: on disk, 0.0 MiB in 1 files"
    assert runner.s3.keys("/latest") == ["s3://bucket/prefix/example/latest"]
    assert runner.s3.keys(".tar.zst")[0].startswith("s3://bucket/prefix/example/")


def test_a_saved_key_is_not_saved_again(runner: Runner) -> None:
    write(runner.home / ".cache/coursier/v1/a.jar", "jar")
    runner.run("save", "--kind", "sbt")
    assert "is already saved" in runner.outcome("save", "--kind", "sbt")


def test_save_warns_when_there_is_nothing_to_save(runner: Runner) -> None:
    assert runner.outcome("save", "--kind", "sbt").startswith(
        "::warning::sbt dependency cache: couldn't save, so other runs restore an older one: none of .cache/coursier"
    )


def test_a_fresh_runner_restores_and_a_warm_one_skips_the_download(runner: Runner) -> None:
    write(runner.home / ".cache/coursier/v1/a.jar", "jar")
    write(runner.home / ".ivy2/cache/x.lock", "lock")
    runner.run("save", "--kind", "sbt")
    fresh = runner.new_runner("second")
    assert "restored " in fresh.outcome("restore", "--kind", "sbt")
    assert (fresh.home / ".cache/coursier/v1/a.jar").read_text() == "jar"
    assert not (fresh.home / ".ivy2/cache/x.lock").exists()
    assert "runner already has " in fresh.outcome("restore", "--kind", "sbt")


def test_another_scope_has_its_own_archive(runner: Runner) -> None:
    write(runner.home / ".cache/coursier/v1/a.jar", "jar")
    runner.run("save", "--kind", "sbt")
    assert "no archive for " in runner.outcome("restore", "--kind", "sbt", S3_CACHE_SCOPE="agg")


def test_a_changed_build_file_falls_back_to_latest(runner: Runner) -> None:
    write(runner.home / ".cache/coursier/v1/a.jar", "jar")
    runner.run("save", "--kind", "sbt")
    runner.tracked["build.sbt"] = "a2"
    assert "already has" in runner.outcome("restore", "--kind", "sbt")
    assert "(newest from main; wanted " in runner.new_runner("second").outcome("restore", "--kind", "sbt")


def test_a_failed_extraction_leaves_nothing_behind(runner: Runner) -> None:
    write(runner.home / ".cache/coursier/v1/a.jar", "jar")
    runner.run("save", "--kind", "sbt")
    broken = Runner(runner.root.parent / "second", runner.s3, BrokenTar(), runner.tracked)
    write(broken.home / ".cache/sbt-dependency-cache/other", "k")
    assert broken.outcome("restore", "--kind", "sbt") == (
        "::warning::sbt dependency cache: couldn't restore, so sbt resolves everything: disk full"
    )
    assert not (broken.home / ".cache/coursier").exists()
    assert not (broken.home / ".cache/sbt-dependency-cache").exists()


def test_an_empty_url_fails(runner: Runner) -> None:
    assert runner.run("restore", "--kind", "sbt", S3_CACHE_URL="") == (
        1,
        [
            (
                "::error::sbt dependency cache: no S3 URL. Pass the action's url input, such as the organization"
                " variable COMMON_GITHUB_BUILD_DEPENDENCY_CACHE."
            )
        ],
    )


def test_mise_saves_its_data_directory_but_not_its_downloads(runner: Runner) -> None:
    mise = runner.home / ".local/share/mise"
    write(mise / "bin/mise", "mise")
    write(mise / "installs/java/21.0.13/bin/java", "jdk")
    (mise / "installs/java/21").symlink_to("21.0.13")
    write(mise / "downloads/jdk.tar.gz", "tarball")
    tools = {"S3_CACHE_TOOLS": "2026.11.10  java"}
    _, lines = runner.run("save", "--kind", "mise", **tools)
    assert "java 21.0.13" in lines
    assert runner.s3.keys("/latest")[0].startswith("s3://bucket/prefix/example/mise-tools/")
    fresh = runner.new_runner("second")
    assert "restored " in fresh.outcome("restore", "--kind", "mise", **tools)
    restored = fresh.home / ".local/share/mise"
    assert (restored / "installs/java/21/bin/java").read_text() == "jdk"
    assert not (restored / "downloads").exists()
    assert "no archive for " in fresh.outcome("restore", "--kind", "mise", S3_CACHE_TOOLS="2026.11.10 sub python uv")


def test_a_mise_data_directory_outside_home_only_warns(runner: Runner) -> None:
    assert runner.outcome(
        "restore", "--kind", "mise", MISE_DATA_DIR="/elsewhere/mise", S3_CACHE_TOOLS="2026.11.10  java"
    ).startswith("::warning::mise tools cache: couldn't restore, so mise downloads its tools: ")


def test_uv_saves_its_cache_but_not_its_builds(runner: Runner) -> None:
    runner.tracked["sub/uv.lock"] = "c3"
    write(runner.home / ".cache/uv/wheels-v5/pypi/requests.whl", "wheel")
    write(runner.home / ".cache/uv/builds-v0/tmp/env", "build")
    runner.run("save", "--kind", "uv", S3_CACHE_SCOPE="sub")
    assert runner.s3.keys("/latest") == ["s3://bucket/prefix/example/uv/sub/latest"]
    fresh = runner.new_runner("second")
    assert "restored " in fresh.outcome("restore", "--kind", "uv", S3_CACHE_SCOPE="sub")
    assert (fresh.home / ".cache/uv/wheels-v5/pypi/requests.whl").read_text() == "wheel"
    assert not (fresh.home / ".cache/uv/builds-v0").exists()
    assert "no archive for " in fresh.outcome("restore", "--kind", "uv")
