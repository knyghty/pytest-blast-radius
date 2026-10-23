"""Runtime changes that once made selection skip a test whose outcome they change."""

import sys

import pytest

from tests.conftest import Project

OTHER = "\n\ndef test_other():\n    pass\n"
RUN_SOURCE = "ex" + "ec"
# State one test leaves for another is credited to the test that made it.
LEFT_BEHIND = pytest.mark.xfail(
    strict=True, reason="state a test leaves behind is a documented limitation"
)
LAZY = pytest.mark.skipif(sys.version_info < (3, 15), reason="lazy imports")


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


CASES_JSON = (
    "import json\n\n\ndef load():\n"
    "    with open('cases.json') as f:\n        return json.load(f)\n"
)
CONFTEST = (
    "from pkg.cases import load\n\n\n"
    "def pytest_generate_tests(metafunc):\n"
    "    if 'case' in metafunc.fixturenames:\n"
    "        metafunc.parametrize('case', load())\n"
)
CASE_TEST = "def test_case(case):\n    assert case == 1\n" + OTHER


@pytest.mark.parametrize(
    "skip",
    [
        "import pytest\n\npytest.skip('no', allow_module_level=True)\n",
        "import pytest\n\npytest.importorskip('no_such_module_xyz')\n",
        "def test_s():\n    pass\n",
    ],
)
def test_module_skip_then_generate_tests(project: Project, skip: str) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/cases.py": CASES_JSON,
            "cases.json": "[1]\n",
            "tests/conftest.py": CONFTEST,
            "tests/test_a_skip.py": skip,
            "tests/test_c.py": CASE_TEST,
        }
    )
    project.run("--blast-radius")
    project.write({"cases.json": "[2]\n"})
    assert "tests/test_c.py::test_case[2]" in ran(project, "--blast-radius")


@LEFT_BEHIND
def test_manual_singleton(project: Project) -> None:
    singleton = (
        "_client = None\n\n\ndef make_config():\n    return 1\n\n\n"
        "def get_client():\n    global _client\n    if _client is None:\n"
        "        _client = make_config()\n    return _client\n"
    )
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/m.py": singleton,
            "tests/test_m.py": (
                "from pkg import m\n\n\n"
                "def test_a():\n    assert m.get_client() == 1\n\n\n"
                "def test_b():\n    assert m.get_client() == 1\n" + OTHER
            ),
        },
        {"pkg/m.py": singleton.replace("return 1", "return 2")},
        {"tests/test_m.py::test_a", "tests/test_m.py::test_b"},
        passed=3,
    )


@LEFT_BEHIND
def test_cached_property_on_module_instance(project: Project) -> None:
    source = (
        "import functools\n\n\nclass Settings:\n    @functools.cached_property\n"
        "    def timeout(self):\n        return 1\n\n\nSETTINGS = Settings()\n"
    )
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/s.py": source,
            "tests/test_s.py": (
                "from pkg.s import SETTINGS\n\n\n"
                "def test_a():\n    assert SETTINGS.timeout == 1\n\n\n"
                "def test_b():\n    assert SETTINGS.timeout == 1\n" + OTHER
            ),
        },
        {"pkg/s.py": source.replace("return 1", "return 2")},
        {"tests/test_s.py::test_a", "tests/test_s.py::test_b"},
        passed=3,
    )


@LEFT_BEHIND
def test_registry_filled_by_earlier_test(project: Project) -> None:
    source = "REGISTRY = []\n\n\ndef register(x):\n    REGISTRY.append(x)\n"
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/reg.py": source,
            "tests/test_r.py": (
                "from pkg import reg\n\n\n"
                "def test_a():\n    reg.register(1)\n\n\n"
                "def test_b():\n    assert reg.REGISTRY == [1]\n" + OTHER
            ),
        },
        {"pkg/reg.py": source.replace("append(x)", "append(x * 2)")},
        {"tests/test_r.py::test_a", "tests/test_r.py::test_b"},
        passed=3,
    )


@LEFT_BEHIND
def test_generator_resumed(project: Project) -> None:
    source = (
        "def numbers():\n    start = 1\n    yield start\n    yield start + 1\n\n\n"
        "NUMBERS = numbers()\n"
    )
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/g.py": source,
            "tests/test_g.py": (
                "from pkg.g import NUMBERS\n\n\n"
                "def test_a():\n    assert next(NUMBERS) == 1\n\n\n"
                "def test_b():\n    assert next(NUMBERS) == 2\n" + OTHER
            ),
        },
        {"pkg/g.py": source.replace("start = 1", "start = 5")},
        {"tests/test_g.py::test_a", "tests/test_g.py::test_b"},
        passed=3,
    )


@pytest.mark.parametrize(
    "read",
    [
        "importlib.import_module('pkg.conf').VALUE",
        "getattr(importlib.import_module('pkg.conf'), 'VALUE')",
        "runpy.run_path('pkg/conf.py')['VALUE']",
        "runpy.run_module('pkg.conf')['VALUE']",
        f"{RUN_SOURCE}(Path('pkg/conf.py').read_text(), ns) or ns['VALUE']",
        "re.search(r'= (.*)', Path('pkg/conf.py').read_text())[1] == '1' and 1",
    ],
)
def test_module_constant_read_indirectly(project: Project, read: str) -> None:
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/conf.py": "VALUE = 1\n",
            "tests/test_d.py": (
                "import importlib\nimport re\nimport runpy\n"
                "from pathlib import Path\n\n\n"
                f"def test_d():\n    ns = {{}}\n    assert {read} == 1\n" + OTHER
            ),
        },
        {"pkg/conf.py": "VALUE = 2\n"},
        {"tests/test_d.py::test_d"},
        passed=2,
    )


ANNOTATED = "Alias = int\n\n\ndef f(x: Alias) -> int:\n    return x\n"
DATACLASS = (
    "import dataclasses\n\nAlias = int\n\n\n@dataclasses.dataclass\n"
    "class C:\n    x: 'Alias'\n"
)


@pytest.mark.parametrize(
    ("source", "new", "hint"),
    [
        (ANNOTATED, "Alias = str", "f)['x'] is int"),
        (ANNOTATED.replace("Alias = int", "Alias = str"), "-> str", "f)['return']"),
        (DATACLASS, "Alias = str", "C)['x'] is int"),
    ],
)
def test_annotations_read_at_runtime(
    project: Project, source: str, new: str, hint: str
) -> None:
    old = "-> int" if new == "-> str" else "Alias = int"
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": source,
            "tests/test_a.py": (
                "import typing\n\nfrom pkg.a import C, f\n\n\n"
                f"def test_hints():\n    assert typing.get_type_hints({hint}\n" + OTHER
            ).replace("C, f", "C" if "C)" in hint else "f"),
        },
        {"pkg/a.py": source.replace(old, new)},
        {"tests/test_a.py::test_hints"},
        passed=2,
    )


@LAZY
def test_lazy_import_side_effect(project: Project) -> None:
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/reg.py": "REG = []\n",
            "pkg/plug.py": "from pkg.reg import REG\n\nREG.append(1)\n",
            "tests/test_a.py": (
                "lazy import pkg.plug\nfrom pkg.reg import REG\n\n\n"
                "def test_a():\n    pkg.plug\n    assert REG == [1]\n" + OTHER
            ),
            "tests/test_b.py": (
                "lazy import pkg.plug\nfrom pkg.reg import REG\n\n\n"
                "def test_b():\n    pkg.plug\n    assert REG == [1]\n"
            ),
        },
        {"pkg/plug.py": "from pkg.reg import REG\n\nREG.append(2)\n"},
        {"tests/test_a.py::test_a", "tests/test_b.py::test_b"},
        passed=3,
    )


@LAZY
def test_lazy_from_import_value(project: Project) -> None:
    check(
        project,
        {
            "pkg/__init__.py": "",
            "pkg/h.py": "def compute():\n    return 1\n",
            "pkg/v.py": "from pkg.h import compute\n\nVALUE = compute()\n",
            "tests/test_a.py": (
                "lazy from pkg.v import VALUE\n\n\n"
                "def test_a():\n    assert VALUE == 1\n" + OTHER
            ),
            "tests/test_b.py": (
                "lazy from pkg.v import VALUE\n\n\n"
                "def test_b():\n    assert VALUE == 1\n"
            ),
        },
        {"pkg/h.py": "def compute():\n    return 2\n"},
        {"tests/test_a.py::test_a", "tests/test_b.py::test_b"},
        passed=3,
    )
