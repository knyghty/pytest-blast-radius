"""functools caches, which recording credits to the tests that hit them."""

from collections.abc import Mapping

import pytest

from tests.conftest import Project

OTHER = "\n\ndef test_other():\n    pass\n"


def ran(project: Project, *args: str) -> set[str]:
    """Return the tests a run called, skipped or failed to set up."""
    reprec = project.pytester.inline_run(*args)
    return {
        report.nodeid
        for report in reprec.getreports("pytest_runtest_logreport")
        if report.when == "call" or report.skipped or report.failed
    }


def selects(
    project: Project,
    files: dict[str, str],
    changes: Mapping[str, str],
    nodeid: str,
    **first: int,
) -> bool:
    """Return whether selection runs a test that a change makes fail."""
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(**first)
    project.write(dict(changes))
    assert project.run().ret != 0
    return nodeid in ran(project, "--blast-radius")


LABEL = (
    "import functools\n\n\n@functools.cache\ndef label(x):\n"
    "    if x:\n        return 'yes'\n    return 'no'\n"
)
HELPER = (
    "import functools\n\n\ndef h(x):\n    if x:\n        return 1\n    return 0\n\n\n"
    "@functools.cache\ndef f():\n    return h(True)\n"
)
CACHED = "import functools\n\n\n@functools.cache\ndef f():\n    return 1\n"
TWO = "from pkg import m\n\n\ndef test_a():\n    assert m.f() == 1\n\n\n"


def test_hit_and_own_lines_in_cached_function(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": LABEL,
        "tests/test_m.py": (
            "from pkg.m import label\n\n\n"
            "def test_a():\n    assert label(True) == 'yes'\n\n\n"
            "def test_b():\n    assert label(True) == 'yes'\n"
            "    assert label(False) == 'no'\n" + OTHER
        ),
    }
    changes = {"pkg/m.py": LABEL.replace("'yes'", "'YES'")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=3)


@pytest.mark.parametrize(
    "tests",
    [
        TWO + "def test_b():\n    assert m.f() == 1\n    assert m.h(False) == 0\n",
        (
            "import pytest\n\nfrom pkg import m\n\n\n"
            "@pytest.fixture(scope='module')\ndef one():\n    return m.h(True)\n\n\n"
            "def test_a(one):\n    assert one == 1\n\n\n"
            "def test_b(one):\n    assert one == 1\n    assert m.h(False) == 0\n"
        ),
    ],
)
def test_hit_and_own_lines_in_helper(project: Project, tests: str) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": HELPER,
        "tests/test_m.py": tests + OTHER,
    }
    changes = {"pkg/m.py": HELPER.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=3)


@pytest.mark.parametrize(
    "source",
    [
        # A cache of a partial.
        (
            "import functools\n\n\ndef compute(x):\n    return x\n\n\n"
            "f = functools.cache(functools.partial(compute, 1))\n"
        ),
        # A cache only a closure holds.
        (
            "import functools\n\n\ndef memo(fn):\n    cached = functools.cache(fn)\n\n"
            "    @functools.wraps(fn)\n    def wrapper(*args):\n"
            "        return cached(*args)\n\n    return wrapper\n\n\n"
            "@memo\ndef f():\n    return 1\n"
        ),
    ],
)
def test_cache_found(project: Project, source: str) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": source,
        "tests/test_m.py": TWO + "def test_b():\n    assert m.f() == 1\n" + OTHER,
    }
    changes = {
        "pkg/m.py": source.replace("return x\n", "return x + 1\n").replace(
            "return 1\n", "return 2\n"
        )
    }
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=3)


def test_cache_of_module_imported_inside_test(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/lazy.py": CACHED,
        "tests/test_m.py": (
            "def test_a():\n    from pkg import lazy\n    assert lazy.f() == 1\n\n\n"
            "def test_b():\n    from pkg import lazy\n    assert lazy.f() == 1\n"
            + OTHER
        ),
    }
    changes = {"pkg/lazy.py": CACHED.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=3)


def test_cache_filled_during_collection(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": CACHED,
        "tests/conftest.py": "from pkg import m\n\nm.f()\n",
        "tests/test_m.py": (
            "from pkg import m\n\n\ndef test_b():\n    assert m.f() == 1\n" + OTHER
        ),
    }
    changes = {"pkg/m.py": CACHED.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=2)


def test_hit_then_cache_clear(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": CACHED,
        "tests/test_m.py": (
            TWO + "def test_x():\n    assert m.f() == 1\n\n\n"
            "def test_b():\n    assert m.f() == 1\n    m.f.cache_clear()\n" + OTHER
        ),
    }
    changes = {"pkg/m.py": CACHED.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=4)


@pytest.mark.xfail(strict=True, reason="state a test leaves behind is a limitation")
def test_cache_applied_at_runtime(project: Project) -> None:
    source = (
        "import functools\n\n\ndef lookup():\n    return 1\n\n\n"
        "def enable_caching():\n    global lookup\n"
        "    lookup = functools.cache(lookup)\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": source,
        "tests/test_m.py": (
            "from pkg import m\n\n\n"
            "def test_a():\n    m.enable_caching()\n    assert m.lookup() == 1\n\n\n"
            "def test_b():\n    assert m.lookup() == 1\n" + OTHER
        ),
    }
    changes = {"pkg/m.py": source.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=3)


def test_hit_at_test_module_import(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": CACHED,
        "tests/test_a.py": (
            "from pkg.m import f\n\nA = f()\n\n\ndef test_a():\n    assert A == 1\n"
        ),
        "tests/test_b.py": (
            "from pkg.m import f\n\nB = f()\n\n\ndef test_b():\n    assert B == 1\n"
            + OTHER
        ),
    }
    changes = {"pkg/m.py": CACHED.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_b.py::test_b", passed=3)


def test_find_spec_probe(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/flags.py": (
            "import importlib.util\n\n\ndef extra():\n"
            "    return importlib.util.find_spec('pkg.extra') is not None\n"
        ),
        "tests/test_f.py": (
            "from pkg.flags import extra\n\n\n"
            "def test_f():\n    assert not extra()\n" + OTHER
        ),
    }
    changes = {"pkg/extra.py": "X = 1\n"}
    assert selects(project, files, changes, "tests/test_f.py::test_f", passed=2)


def test_exception_in_cached_function_then_hit(project: Project) -> None:
    source = (
        "import functools\n\n\ndef base():\n    return 1\n\n\n"
        "@functools.cache\ndef bad():\n    raise ValueError\n\n\n"
        "@functools.cache\ndef f():\n    try:\n        bad()\n"
        "    except ValueError:\n        pass\n    return base()\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": source,
        "tests/test_m.py": TWO + "def test_b():\n    assert m.f() == 1\n" + OTHER,
    }
    changes = {"pkg/m.py": source.replace("return 1", "return 2")}
    assert selects(project, files, changes, "tests/test_m.py::test_b", passed=3)
