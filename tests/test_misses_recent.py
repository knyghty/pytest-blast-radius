"""Gaps and outcome changes once found in the newest recording and selection code."""

import pytest

from tests.conftest import Project
from tests.test_misses import DJANGO
from tests.test_misses import SETTINGS

OTHER = "\n\ndef test_other():\n    pass\n"


def ran(project: Project, *args: str) -> set[str]:
    """Return the tests a run called or skipped."""
    reprec = project.pytester.inline_run(*args)
    return {
        report.nodeid
        for report in reprec.getreports("pytest_runtest_logreport")
        if report.when == "call" or report.skipped
    }


def selects(
    project: Project,
    files: dict[str, str],
    changes: dict[str, str],
    nodeid: str,
    **first: int,
) -> bool:
    """Return whether selection runs a test that a change makes fail."""
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(**first)
    project.write(changes)
    assert project.run().ret != 0
    return nodeid in ran(project, "--blast-radius")


def test_root_directory_listing(project: Project) -> None:
    files = {
        "a.json": "{}",
        "tests/test_g.py": (
            "from pathlib import Path\n\n\n"
            "def test_g():\n"
            "    assert [p.name for p in Path('.').glob('*.json')] == ['a.json']\n"
            + OTHER
        ),
    }
    assert selects(
        project, files, {"b.json": "{}"}, "tests/test_g.py::test_g", passed=2
    )


def test_directory_listed_at_import(project: Project) -> None:
    files = {
        "cases/a.txt": "a",
        "pkg/__init__.py": "",
        "pkg/data.py": (
            "from pathlib import Path\n\nFILES = sorted(p.name for p in "
            "(Path(__file__).parent.parent / 'cases').glob('*.txt'))\n"
        ),
        "tests/test_d.py": (
            "from pkg.data import FILES\n\n\n"
            "def test_d():\n    assert FILES == ['a.txt']\n" + OTHER
        ),
    }
    assert selects(
        project, files, {"cases/b.txt": "b"}, "tests/test_d.py::test_d", passed=2
    )


def test_directory_existence_checked(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/flags.py": (
            "import os\n\n\ndef enabled():\n    return os.path.isdir('overrides')\n"
        ),
        "tests/test_f.py": (
            "from pkg.flags import enabled\n\n\n"
            "def test_f():\n    assert not enabled()\n" + OTHER
        ),
    }
    assert selects(
        project, files, {"overrides/x.txt": "x"}, "tests/test_f.py::test_f", passed=2
    )


def test_symlinked_data_file_retargeted(project: Project) -> None:
    files = {
        "config/prod.json": "1",
        "config/dev.json": "2",
        "tests/test_c.py": (
            "from pathlib import Path\n\n\ndef test_c():\n"
            "    assert Path('config/current.json').read_text() == '1'\n" + OTHER
        ),
    }
    project.write(files)
    link = project.path / "config" / "current.json"
    link.symlink_to("prod.json")
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=2)
    link.unlink()
    link.symlink_to("dev.json")
    assert "tests/test_c.py::test_c" in ran(project, "--blast-radius")


def test_getsource_of_unrun_function(project: Project) -> None:
    source = "def described():\n    return 'old'\n"
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": source,
        "tests/test_s.py": (
            "import inspect\n\nfrom pkg import m\n\n\ndef test_s():\n"
            "    assert \"'old'\" in inspect.getsource(m.described)\n" + OTHER
        ),
    }
    assert selects(
        project,
        files,
        {"pkg/m.py": source.replace("'old'", "'new'")},
        "tests/test_s.py::test_s",
        passed=2,
    )


def test_enum_with_project_enum_base(project: Project) -> None:
    source = (
        "import enum\n\n\nclass Base(enum.Enum):\n    pass\n\n\n"
        "class Color(Base):\n    RED = 1\n\n\nLABELS = [c.name for c in Color]\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/e.py": source,
        "tests/test_e.py": (
            "from pkg.e import LABELS\n\n\n"
            "def test_e():\n    assert LABELS == ['RED']\n" + OTHER
        ),
    }
    assert selects(
        project,
        files,
        {"pkg/e.py": source.replace("RED = 1\n", "RED = 1\n    BLUE = 2\n")},
        "tests/test_e.py::test_e",
        passed=2,
    )


@pytest.mark.parametrize(
    "added",
    [
        "from suite.helpers import value  # noqa\n",
        "import pytest\n\nvalue = pytest.fixture(lambda: 2, name='value')\n",
    ],
)
def test_fixture_bound_without_def(project: Project, added: str) -> None:
    fixture = "import pytest\n\n\n@pytest.fixture\ndef value():\n    return {}\n"
    files = {
        "suite/__init__.py": "",
        "suite/helpers.py": fixture.format(2),
        "suite/conftest.py": fixture.format(1),
        "suite/sub/__init__.py": "",
        "suite/sub/conftest.py": "X = 1\n",
        "suite/sub/test_v.py": "def test_v(value):\n    assert value == 1\n" + OTHER,
    }
    assert selects(
        project,
        files,
        {"suite/sub/conftest.py": "X = 1\n" + added},
        "suite/sub/test_v.py::test_v",
        passed=2,
    )


def test_django_cache_filled_by_module_fixture(project: Project) -> None:
    tests = (
        "import pytest\nfrom django.core.cache import cache\n\n\n"
        "@pytest.fixture(scope='module')\ndef warm():\n    cache.set('k', 1)\n\n\n"
        "def test_a(warm):\n    assert cache.get('k') == 1\n\n\n"
        "def test_b(warm):\n    assert cache.get('k') == 1\n"
    )
    project.commit({**DJANGO, "tests/test_t.py": tests})
    project.run("--blast-radius").assert_outcomes(passed=2)


def test_functools_cache_filled_by_module_fixture(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/r.py": (
                "import functools\n\n\n@functools.cache\ndef registry():\n"
                "    return {}\n"
            ),
            "tests/test_r.py": (
                "import pytest\n\nfrom pkg.r import registry\n\n\n"
                "@pytest.fixture(scope='module')\ndef filled():\n"
                "    registry()['k'] = 1\n\n\n"
                "def test_a(filled):\n    assert registry()['k'] == 1\n\n\n"
                "def test_b(filled):\n    assert registry()['k'] == 1\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)


def test_noconftest_runs_again(project: Project) -> None:
    project.commit(
        {
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 1\n"
            ),
            "tests/test_v.py": "def test_v(value):\n    assert value == 1\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    assert project.run("--blast-radius", "--noconftest").ret != 0


def test_copy2_of_a_symlink(project: Project) -> None:
    project.commit(
        {
            "tests/test_c.py": (
                "import os\nimport shutil\n\n\ndef test_c(tmp_path):\n"
                "    (tmp_path / 'a').write_text('x')\n"
                "    os.symlink(tmp_path / 'a', tmp_path / 'link')\n"
                "    shutil.copy2(\n"
                "        str(tmp_path / 'link'), str(tmp_path / 'dst'),"
                " follow_symlinks=False\n    )\n"
            )
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)


def test_date_format_cached(project: Project) -> None:
    check = (
        "    date = datetime.date(2020, 1, 2)\n"
        "    assert formats.date_format(date, use_l10n=False) == '2020-01-02'\n"
    )
    tests = (
        "import datetime\n\nfrom django.utils import formats\n\n\n"
        f"def test_a():\n{check}\n\ndef test_b():\n{check}" + OTHER
    )
    settings = SETTINGS + "DATE_FORMAT = 'Y-m-d'\n"
    files = {**DJANGO, "site_/settings.py": settings, "tests/test_t.py": tests}
    assert selects(
        project,
        files,
        {"site_/settings.py": settings.replace("Y-m-d", "d/m/Y")},
        "tests/test_t.py::test_b",
        passed=3,
    )
