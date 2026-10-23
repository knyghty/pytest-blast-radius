import gzip
import json
import os
import subprocess
import sys
import textwrap

import pytest

PYPROJECT = """\
[tool.pytest.ini_options]
pythonpath = ["."]
"""


@pytest.fixture(autouse=True)
def _forget_django() -> object:
    """Unload Django after a test that loaded it, so later runs start afresh."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "django" or name.startswith(
            ("django.", "pytest_blast_radius.django_hooks")
        ):
            del sys.modules[name]


class Project:
    """A git repository of tests to record and select from."""

    def __init__(self, pytester: pytest.Pytester) -> None:
        self.pytester = pytester
        self.path = pytester.path
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=self.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def write(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content))

    def commit(self, files: dict[str, str] | None = None) -> None:
        # Subprocess runs write these files, which would make the tree dirty.
        defaults = {
            "pyproject.toml": PYPROJECT,
            ".gitignore": "stdout\nstderr\nrunpytest-*\n.coverage*\n",
        }
        self.write({**defaults, **(files or {})})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Change files")

    def run(self, *args: str) -> pytest.RunResult:
        return self.pytester.runpytest(*args)

    def ran(self, *args: str) -> set[str]:
        """Return the tests a run called."""
        reprec = self.pytester.inline_run(*args)
        return {
            report.nodeid
            for report in reprec.getreports("pytest_runtest_logreport")
            if report.when == "call"
        }

    def maps(self) -> list[dict]:
        """Return the recordings, oldest first."""
        directory = self.path / ".git" / "blast-radius"
        return [
            json.loads(gzip.decompress(path.read_bytes()))
            for path in sorted(
                directory.glob("*.json.gz"), key=lambda path: path.stat().st_mtime_ns
            )
        ]


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # Edits within a second can keep a file's size and mtime, so stale bytecode loads.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return Project(pytester)
