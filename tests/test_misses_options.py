"""Pytest options that once made a run skip a test or keep a stale failure."""

from tests.conftest import Project

OTHER = "\n\ndef test_other():\n    pass\n"
MOD = {"pkg/__init__.py": "", "pkg/mod.py": "def f():\n    return 1\n"}


def test_last_failed_narrows_collected(project: Project) -> None:
    test = (
        "from pkg import mod\n\n\ndef test_fail():\n    assert False\n\n\n"
        "def test_b():\n    assert mod.f() == 1\n"
    )
    project.commit({**MOD, "tests/test_a.py": test})
    project.run("--blast-radius").assert_outcomes(passed=1, failed=1)
    project.write({"tests/test_a.py": test.replace("assert False", "assert True")})
    project.run("--blast-radius", "--lf").assert_outcomes(passed=1)
    project.write({"pkg/mod.py": "def f():\n    return 2\n"})
    project.run("--blast-radius").assert_outcomes(failed=1)


def test_no_terminal_keeps_failures(project: Project) -> None:
    project.commit({"tests/test_a.py": "def test_a():\n    assert False\n" + OTHER})
    project.pytester.inline_run("--blast-radius", "-p", "no:terminal")
    assert project.run("--blast-radius").ret == 1


def test_setup_phase_importorskip(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/helpers.py": "X = 1\n",
            "pkg/feature.py": (
                "from pkg.helpers import thing\n\n\ndef run():\n    return thing()\n"
            ),
            "tests/test_f.py": (
                "import pytest\n\nimport pkg.helpers\n\n\n"
                "@pytest.fixture\ndef feature():\n"
                "    return pytest.importorskip(\n"
                "        'pkg.feature', exc_type=ImportError\n    )\n"
                "\n\ndef test_feature(feature):\n    assert feature.run() == 1\n"
                + OTHER
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1, skipped=1)
    project.write({"pkg/helpers.py": "X = 1\n\n\ndef thing():\n    return 2\n"})
    project.run("--blast-radius").assert_outcomes(failed=1)


def test_setup_only_records_nothing(project: Project) -> None:
    project.commit(
        {
            **MOD,
            "tests/test_a.py": (
                "from pkg import mod\n\n\ndef test_a():\n    assert mod.f() == 1\n"
                + OTHER
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/mod.py": "def f():\n    return 2\n"})
    project.run("--blast-radius", "--setup-only")
    project.run("--blast-radius").assert_outcomes(failed=1)


def test_warnings_as_errors_not_reused(project: Project) -> None:
    project.commit(
        {
            "tests/test_w.py": (
                "import warnings\n\n\n"
                "def test_w():\n    warnings.warn('x', DeprecationWarning)\n" + OTHER
            )
        }
    )
    result = project.run("--blast-radius", "-W", "error::DeprecationWarning")
    result.assert_outcomes(passed=1, failed=1)
    assert project.run("--blast-radius").ret == 0


def test_runxfail_runs_expected_failures(project: Project) -> None:
    project.commit(
        {
            "tests/test_x.py": (
                "import pytest\n\n\n@pytest.mark.xfail\n"
                "def test_x():\n    assert False\n" + OTHER
            )
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1, xfailed=1)
    assert project.run("--blast-radius", "--runxfail").ret == 1
