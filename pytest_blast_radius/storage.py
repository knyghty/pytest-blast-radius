"""Talk to git, and load and save recorded runs."""

import functools
import gzip
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Bump when the recording format or what parsing a module returns changes.
FORMAT_VERSION = "27"
KEEP = 100
# Marks a module the project imports that wasn't installed, in a run's environment.
ABSENT = "absent:"


class GitError(Exception):
    """Raise when git fails or the directory is not in a repository."""


@dataclass(frozen=True)
class Repository:
    """A git work tree, the directory its worktrees share and its index."""

    root: Path
    common: Path
    index: Path

    def git(
        self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None
    ) -> str:
        """Run git in the work tree and return its output."""
        return run_git(self.root, *args, env=env, stdin=stdin)

    def snapshot(self) -> str:
        """Return the git tree of the work tree, with untracked but not ignored files.

        Git hashes the files into a copy of the index, so HEAD and the index stay put.
        The copy keeps the index's mtime, which git needs to spot files changed in the
        same second the index was written. Files git is told to assume unchanged or
        to skip count as they are on disk, clean filters are bypassed, and a nested
        repository counts with its uncommitted changes to tracked files.
        """
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "index"
            if self.index.exists():
                shutil.copy2(self.index, temporary)
            env = {**os.environ, "GIT_INDEX_FILE": str(temporary)}
            # Lowercase tags mark files assumed unchanged, and "S" skipped ones.
            flagged = [
                entry[2:]
                for entry in self.git("ls-files", "-v", "-z", env=env).split("\0")
                if entry[:1].islower() or entry[:1] == "S"
            ]
            if on_disk := [path for path in flagged if (self.root / path).exists()]:
                # Git applies only the last of these flags given together.
                for flag in ("--no-assume-unchanged", "--no-skip-worktree"):
                    self.git(
                        "update-index",
                        flag,
                        "-z",
                        "--stdin",
                        env=env,
                        stdin="".join(f"{path}\0" for path in on_disk),
                    )
            self.git(*self._unfiltered(), "add", "-A", env=env)
            for entry in self.git("ls-files", "-s", "-z", env=env).split("\0"):
                if entry.startswith("160000 "):
                    path = entry.partition("\t")[2]
                    self._nested(path, env)
            return self.git("write-tree", env=env).strip()

    def _unfiltered(self) -> list[str]:
        """Return options that turn the configured clean filters off."""
        try:
            keys = self.git(
                "config", "--name-only", "--get-regexp", r"^filter\..*\.clean$"
            )
        except GitError:
            return []
        return [option for key in keys.split() for option in ("-c", f"{key}=cat")]

    def _nested(self, path: str, env: dict[str, str]) -> None:
        """Point a nested repository's entry at a commit holding its changes."""
        # An uninitialised submodule's empty directory belongs to this repository.
        if not (self.root / path / ".git").exists():
            return
        if commit := run_git(self.root / path, "stash", "create").strip():
            self.git("update-index", "--cacheinfo", f"160000,{commit},{path}", env=env)

    def source(self, tree: str, path: str) -> str | None:
        """Return a file's text in a tree, or None if it is not there."""
        try:
            return self.git("show", f"{tree}:{path}")
        except GitError:
            return None

    @property
    def directory(self) -> Path:
        """Return where recorded runs live."""
        return self.common / "blast-radius"

    def load(self, tree: str, packages: dict[str, str]) -> dict[str, Any] | None:
        """Return the usable run recorded closest to a tree.

        A run is usable when the Python and packages it used are installed now.
        Closest means the fewest changed files. Trees git has pruned are skipped.
        Loading a run marks it as recently used, so it is kept.
        """
        candidates: dict[str, Path] = {}
        findable = functools.cache(_findable)
        for path in sorted(self._maps(), key=_mtime, reverse=True):
            try:
                used = json.loads(_environment_path(path).read_text())
            except (OSError, ValueError):
                continue
            if _usable(used, packages, findable):
                tree_name = path.name.removesuffix(".json.gz").rpartition("-")[2]
                candidates.setdefault(tree_name, path)
        # One git process diffs every pair. It prints each pair, ending in a newline,
        # then each changed file, ending in a NUL, and skips pairs it can't read.
        headers = {f"{name} {tree}\n": name for name in candidates}
        size = len(tree) * 2 + 2
        output = self.git(
            "diff-tree",
            "--stdin",
            "-r",
            "--name-only",
            "--no-renames",
            "-z",
            stdin="".join(headers),
        )
        changed: dict[str, int] = {}
        current = ""
        for chunk in output.split("\0"):
            while chunk[:size] in headers:
                current = headers[chunk[:size]]
                changed[current] = 0
                chunk = chunk[size:]  # noqa: PLW2901
            if chunk:
                changed[current] += 1
        if not changed:
            return None
        # Ties go to the most recently used run.
        path = candidates[min(changed, key=changed.__getitem__)]
        path.touch()
        return json.loads(gzip.decompress(path.read_bytes()))

    def save(self, data: dict[str, Any]) -> None:
        """Store a recorded run and drop all but the most recently used ones."""
        self.directory.mkdir(parents=True, exist_ok=True)
        python = hashlib.sha256(data["environment"]["python"].encode()).hexdigest()
        path = self.directory / f"{python[:12]}-{data['tree']}.json.gz"
        suffix = f".{os.getpid()}.tmp"
        environment = _environment_path(path)
        _replace(environment, json.dumps(data["environment"]).encode(), suffix)
        _replace(path, gzip.compress(json.dumps(data).encode(), 6), suffix)
        for old in sorted(self._maps(), key=_mtime)[:-KEEP]:
            old.unlink(missing_ok=True)
            _environment_path(old).unlink(missing_ok=True)

    @property
    def _parsed_path(self) -> Path:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        return self.directory / f"parsed-{FORMAT_VERSION}-{python}.gz"

    def load_parsed(self) -> dict[str, Any]:
        """Return the modules the last run parsed, or nothing if it is unreadable."""
        try:
            return json.loads(gzip.decompress(self._parsed_path.read_bytes()))
        except (OSError, EOFError, ValueError):
            return {}

    def save_parsed(self, parsed: dict[str, Any]) -> None:
        """Store the modules this run parsed, for the next run, and drop the ones
        other formats parsed."""
        content = gzip.compress(json.dumps(parsed).encode(), 1)
        _replace(self._parsed_path, content, f".{os.getpid()}.tmp")
        for old in self.directory.glob("parsed-*.gz"):
            if not old.name.startswith(f"parsed-{FORMAT_VERSION}-"):
                old.unlink(missing_ok=True)

    def _maps(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return list(self.directory.glob("*.json.gz"))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _environment_path(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(".json.gz") + ".environment.json")


def _replace(path: Path, content: bytes, suffix: str) -> None:
    temporary = path.with_name(path.name + suffix)
    temporary.write_bytes(content)
    temporary.replace(path)


def run_git(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    """Run git and return its output, raising GitError on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-c", "core.quotepath=off", *args],  # noqa: S607
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            env=env,
            check=False,
        )
    except OSError as error:
        raise GitError(str(error)) from error
    if result.returncode:
        raise GitError(result.stderr.strip())
    return result.stdout


def find_repository(path: Path) -> Repository:
    """Return the repository containing a path."""
    root, common, index = run_git(
        path,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-common-dir",
        "--git-path",
        "index",
    ).splitlines()
    return Repository(Path(root), Path(common), Path(index))


def installed() -> dict[str, str]:
    """Return the Python version and the version of each installed distribution.

    It includes the recording format, so recordings in another format are skipped.
    """
    return {
        "python": sys.version,
        "format": FORMAT_VERSION,
        **{
            distribution.name: distribution.version
            for distribution in importlib.metadata.distributions()
        },
    }


def environment(
    modules: Iterable[str], packages: dict[str, str], imports: Iterable[str] = ()
) -> dict[str, str]:
    """Return the Python version and the versions of the distributions a run used.

    A run uses the distributions of the modules it imported, and every pytest
    plugin, which loads without the tests importing it. It also notes the modules
    the project imports that aren't installed, as optional ones: installing one
    changes what runs.
    """
    absent = {
        f"{ABSENT}{name}": ""
        for name in set(imports)
        if name not in sys.modules and not _findable(name)
    }
    providers = importlib.metadata.packages_distributions()
    used = {
        name
        for module in modules
        for name in providers.get(module.partition(".")[0], [])
    }
    used.update(
        entry_point.dist.name
        for entry_point in importlib.metadata.entry_points(group="pytest11")
        if entry_point.dist is not None
    )
    return {
        **{
            name: version
            for name, version in packages.items()
            if name in {"python", "format", "options"} or name in used
        },
        **absent,
    }


def _findable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _usable(
    used: dict[str, str], packages: dict[str, str], findable: Callable[[str], bool]
) -> bool:
    """Return whether what a run used is installed now, and what it lacked isn't."""
    return used.get("format") == FORMAT_VERSION and all(
        not findable(name.removeprefix(ABSENT))
        if name.startswith(ABSENT)
        else packages.get(name) == version
        for name, version in used.items()
    )
