"""More changes that once made selection skip a test whose outcome they change."""

import sqlite3

from tests.conftest import Project

OTHER = "\n\ndef test_other():\n    pass\n"


def ran(project: Project, *args: str) -> set[str]:
    """Return the tests a run called or skipped."""
    reprec = project.pytester.inline_run(*args)
    return {
        report.nodeid
        for report in reprec.getreports("pytest_runtest_logreport")
        if report.when == "call" or report.skipped
    }


def check(
    project: Project,
    files: dict[str, str],
    changes: dict[str, str],
    expected: set[str],
    **first: int,
) -> None:
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(**first)
    project.write(changes)
    assert expected <= ran(project, "--blast-radius")


CONF = "LIMIT = 1\n\n\ndef limit():\n    return LIMIT\n"
USE = {
    "pkg/__init__.py": "",
    "pkg/conf.py": CONF,
    "pkg/use.py": "from pkg.conf import limit\n\nVALUE = limit()\n",
    "tests/test_u.py": (
        "from pkg.use import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n"
        + OTHER
    ),
}


def test_value_read_by_import_time_function(project: Project) -> None:
    check(
        project,
        USE,
        {"pkg/conf.py": CONF.replace("= 1", "= 2")},
        {"tests/test_u.py::test_value"},
        passed=2,
    )


def test_import_time_function_changed(project: Project) -> None:
    check(
        project,
        USE,
        {"pkg/conf.py": CONF.replace("return LIMIT", "return LIMIT + 1")},
        {"tests/test_u.py::test_value"},
        passed=2,
    )


def test_generate_tests_reads_constant(project: Project) -> None:
    cases = "CASES = [{'v': 1}]\n\n\ndef load_cases():\n    return list(CASES)\n"
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/cases.py": cases,
            "tests/conftest.py": (
                "from pkg.cases import load_cases\n\n\n"
                "def pytest_generate_tests(metafunc):\n"
                "    if 'case' in metafunc.fixturenames:\n"
                "        metafunc.parametrize('case', load_cases())\n"
            ),
            "tests/test_c.py": "def test_case(case):\n    assert case['v'] == 1\n"
            + OTHER,
        },
        {"pkg/cases.py": cases.replace("1", "2")},
        {"tests/test_c.py::test_case[case0]"},
        passed=2,
    )


def test_skipif_from_project_function(project: Project) -> None:
    feature = "def enabled():\n    return False\n"
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/feature.py": feature,
            "tests/test_s.py": (
                "import pytest\n\nfrom pkg.feature import enabled\n\n\n"
                "@pytest.mark.skipif(not enabled(), reason='off')\n"
                "def test_on():\n    assert False\n" + OTHER
            ),
        },
        {"pkg/feature.py": feature.replace("False", "True")},
        {"tests/test_s.py::test_on"},
        passed=1,
        skipped=1,
    )


def test_skipif_from_project_constant(project: Project) -> None:
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/feature.py": "ENABLED = False\n",
            "tests/test_s.py": (
                "import pytest\n\nfrom pkg import feature\n\n\n"
                "@pytest.mark.skipif(not feature.ENABLED, reason='off')\n"
                "def test_on():\n    assert False\n" + OTHER
            ),
        },
        {"pkg/feature.py": "ENABLED = True\n"},
        {"tests/test_s.py::test_on"},
        passed=1,
        skipped=1,
    )


def test_pytestmark_from_project_constant(project: Project) -> None:
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/feature.py": "ENABLED = False\n",
            "tests/test_s.py": (
                "import pytest\n\nfrom pkg import feature\n\n"
                "pytestmark = pytest.mark.skipif(not feature.ENABLED, reason='off')"
                "\n\n\ndef test_on():\n    assert False\n"
            ),
            "tests/test_o.py": OTHER,
        },
        {"pkg/feature.py": "ENABLED = True\n"},
        {"tests/test_s.py::test_on"},
        passed=1,
        skipped=1,
    )


def test_added_file_in_listed_directory(project: Project) -> None:
    check(
        project,
        {
            "cases/a.txt": "ok",
            "tests/test_d.py": (
                "from pathlib import Path\n\n\n"
                "def test_all():\n"
                "    root = Path(__file__).parent.parent / 'cases'\n"
                "    for path in sorted(root.glob('*.txt')):\n"
                "        assert path.read_text() == 'ok'\n" + OTHER
            ),
        },
        {"cases/b.txt": "bad"},
        {"tests/test_d.py::test_all"},
        passed=2,
    )


def test_added_plugin_module(project: Project) -> None:
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/plugins/__init__.py": (
                "import importlib\nimport pkgutil\n\n\n"
                "def load():\n"
                "    return [\n"
                "        importlib.import_module(f'{__name__}.{info.name}').NAME\n"
                "        for info in pkgutil.iter_modules(__path__)\n"
                "    ]\n"
            ),
            "pkg/plugins/a.py": "NAME = 'a'\n",
            "tests/test_p.py": (
                "from pkg import plugins\n\n\n"
                "def test_load():\n    assert plugins.load() == ['a']\n" + OTHER
            ),
        },
        {"pkg/plugins/b.py": "NAME = 'b'\n"},
        {"tests/test_p.py::test_load"},
        passed=2,
    )


def test_sqlite_file(project: Project) -> None:
    database = project.path / "data.db"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("create table t (v)")
        connection.execute("insert into t values (1)")
    connection.close()
    project.commit(
        {
            "tests/test_q.py": """\
                import sqlite3
                from pathlib import Path


                def test_db():
                    path = Path(__file__).parent.parent / "data.db"
                    connection = sqlite3.connect(path)
                    rows = connection.execute("select v from t").fetchall()
                    connection.close()
                    assert rows == [(1,)]


                def test_other():
                    pass
                """,
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("update t set v = 2")
    connection.close()
    assert "tests/test_q.py::test_db" in ran(project, "--blast-radius")


def test_all_narrowed_under_star_import(project: Project) -> None:
    impl = (
        "__all__ = ['a', 'b']\n\n\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n"
    )
    project.commit(
        {
            "pkg/__init__.py": "from pkg.impl import *\n",
            "pkg/impl.py": impl,
            "tests/test_b.py": "from pkg import b\n\n\ndef test_b():\n    assert b()\n",
            "tests/test_o.py": OTHER,
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/impl.py": impl.replace("['a', 'b']", "['a']")})
    # test_b.py no longer imports.
    project.run("--blast-radius").assert_outcomes(errors=1)


FEATURE = {
    "pkg/__init__.py": "",
    "pkg/helpers.py": "X = 1\n",
    "pkg/feature.py": (
        "from pkg.helpers import thing\n\n\ndef run():\n    return thing()\n"
    ),
}
HELPERS = {"pkg/helpers.py": "X = 1\n\n\ndef thing():\n    return 2\n"}


def test_importorskip_project_module(project: Project) -> None:
    check(
        project,
        {
            **FEATURE,
            "tests/test_f.py": (
                "import pytest\n\nimport pkg.helpers\n\n\n"
                "def test_feature():\n"
                "    feature = pytest.importorskip(\n"
                "        'pkg.feature', exc_type=ImportError\n    )\n"
                "    assert feature.run() == 1\n" + OTHER
            ),
        },
        HELPERS,
        {"tests/test_f.py::test_feature"},
        passed=1,
        skipped=1,
    )


def test_xfail_cut_short(project: Project) -> None:
    check(
        project,
        {
            **FEATURE,
            "tests/test_f.py": (
                "import importlib\n\nimport pytest\n\nimport pkg.helpers\n\n\n"
                "@pytest.mark.xfail(strict=True)\n"
                "def test_feature():\n"
                "    feature = importlib.import_module('pkg.feature')\n"
                "    assert feature.run() == 2\n" + OTHER
            ),
        },
        HELPERS,
        {"tests/test_f.py::test_feature"},
        passed=1,
        xfailed=1,
    )
