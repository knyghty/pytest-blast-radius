"""Changes git hides from a plain `git add`, and failures that can't be reused."""

import subprocess
from pathlib import Path

import pytest

from tests.conftest import Project
from tests.test_misses_caches import OTHER
from tests.test_misses_caches import ran

READS = (
    "from pathlib import Path\n\n\ndef test_c():\n"
    "    assert Path('local.cfg').read_text() == '1'\n" + OTHER
)
VALUE = "from pkg.m import value\n\n\ndef test_v():\n    assert value() == 1\n" + OTHER
LIB = '[tool.pytest.ini_options]\npythonpath = ["lib"]\n'


def git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "protocol.file.allow=always", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def library(path: Path) -> None:
    """Make a repository holding pkg.m.value()."""
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "__init__.py").write_text("")
    (path / "pkg" / "m.py").write_text("def value():\n    return 1\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "1")


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_flagged_file(project: Project, flag: str) -> None:
    project.commit({"local.cfg": "1", "tests/test_c.py": READS})
    project.git("update-index", flag, "local.cfg")
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"local.cfg": "2"})
    assert project.run().ret != 0
    assert "tests/test_c.py::test_c" in ran(project, "--blast-radius")


def test_clean_filter(project: Project) -> None:
    project.git("config", "filter.strip.clean", "sed s/=.*/=/")
    project.git("config", "filter.strip.smudge", "cat")
    project.commit(
        {
            ".gitattributes": "local.cfg filter=strip\n",
            "local.cfg": "key=1",
            "tests/test_c.py": READS.replace("'1'", "'key=1'"),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"local.cfg": "key=2"})
    assert project.run().ret != 0
    assert "tests/test_c.py::test_c" in ran(project, "--blast-radius")


def test_textconv(project: Project) -> None:
    source = "def value():\n    return 1\n\n\ndef other():\n    return 1\n"
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": source,
            ".gitattributes": "*.py diff=strip\n",
            "tests/test_v.py": VALUE.replace("value", "other"),
        }
    )
    # Dropping blank lines would shift every hunk's line numbers.
    project.git("config", "diff.strip.textconv", "grep -v '^$'")
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/m.py": source[:-2] + "2\n"})
    assert project.run().ret != 0
    assert "tests/test_v.py::test_v" in ran(project, "--blast-radius")


def test_nested_repository_changed(project: Project) -> None:
    library(project.path / "lib")
    project.commit({"pyproject.toml": LIB, "tests/test_v.py": VALUE})
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"lib/pkg/m.py": "def value():\n    return 2\n"})
    assert project.run().ret != 0
    assert "tests/test_v.py::test_v" in ran(project, "--blast-radius")


def test_submodule_updated(project: Project) -> None:
    upstream = project.path.parent / f"{project.path.name}-upstream"
    library(upstream)
    git(project.path, "submodule", "add", "-q", str(upstream), "lib")
    project.commit({"pyproject.toml": LIB, "tests/test_v.py": VALUE})
    project.run("--blast-radius").assert_outcomes(passed=2)
    (upstream / "pkg" / "m.py").write_text("def value():\n    return 2\n")
    git(upstream, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "2")
    git(project.path / "lib", "pull", "-q", "origin", "main")
    assert project.run().ret != 0
    assert "tests/test_v.py::test_v" in ran(project, "--blast-radius")


def test_submodule_not_checked_out(project: Project) -> None:
    upstream = project.path.parent / f"{project.path.name}-upstream"
    library(upstream)
    git(project.path, "submodule", "add", "-q", str(upstream), "lib")
    project.commit({"tests/test_o.py": "def test_o():\n    pass\n"})
    git(project.path, "submodule", "deinit", "--force", "lib")
    project.run("--blast-radius").assert_outcomes(passed=1)
    result = project.run("--blast-radius")
    result.assert_outcomes()
    assert "unaffected by 0 changed files" in result.stdout.str()


@pytest.mark.parametrize(
    ("link", "files", "tests"),
    [
        pytest.param(
            "data",
            {"v1/x.txt": "1", "v2/x.txt": "2"},
            "from pathlib import Path\n\n\ndef test_v():\n"
            "    assert Path('data/x.txt').read_text() == '1'\n",
            id="data",
        ),
        pytest.param(
            "pkg",
            {
                "v1/__init__.py": "",
                "v1/m.py": "def value():\n    return 1\n",
                "v2/__init__.py": "",
                "v2/m.py": "def value():\n    return 2\n",
            },
            VALUE,
            id="package",
        ),
    ],
)
def test_directory_link_retargeted(
    project: Project, link: str, files: dict[str, str], tests: str
) -> None:
    files = {**files, "tests/test_v.py": tests + OTHER}
    project.write(files)
    (project.path / link).symlink_to("v1")
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=2)
    (project.path / link).unlink()
    (project.path / link).symlink_to("v2")
    assert project.run().ret != 0
    assert "tests/test_v.py::test_v" in ran(project, "--blast-radius")


def test_failure_in_narrowed_run_reruns(project: Project) -> None:
    project.commit(
        {
            "tests/test_s.py": (
                "STATE = []\n\n\ndef test_a():\n    STATE.append(1)\n\n\n"
                "def test_b():\n    assert STATE\n"
            )
        }
    )
    project.run("--blast-radius-record", "-k", "test_b").assert_outcomes(failed=1)
    project.run("--blast-radius").assert_outcomes(passed=2)


def test_setup_error_reruns(project: Project) -> None:
    project.commit(
        {
            "tests/test_e.py": (
                "import pytest\n\n\n@pytest.fixture\ndef broken():\n"
                "    raise RuntimeError\n\n\ndef test_e(broken):\n    pass\n" + OTHER
            ),
            "tests/test_o.py": "def test_o():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2, errors=1)
    project.write({"tests/test_o.py": "def test_o():\n    assert 1\n"})
    project.run("--blast-radius").assert_outcomes(passed=1, errors=1)


def test_failure_named_from_subdirectory_reruns(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.commit(
        {
            "tests/test_f.py": "def test_f():\n    assert False\n" + OTHER,
            "tests/test_o.py": "def test_o():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2, failed=1)
    monkeypatch.chdir(project.path / "tests")
    result = project.run("--blast-radius", "test_f.py")
    result.assert_outcomes(failed=1)
    assert "reused this" not in result.stdout.str()
