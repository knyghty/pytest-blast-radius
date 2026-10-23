"""Top-level statements that bind or change more than their targets show."""

import pytest

from tests.conftest import Project
from tests.test_misses_caches import OTHER
from tests.test_misses_caches import selects

GETX = "\n\ndef getx():\n    return X\n"


@pytest.mark.parametrize(
    ("source", "old", "new"),
    [
        pytest.param("Y = (X := 5) + 1\n" + GETX, "5", "6", id="assignment"),
        pytest.param(
            "def f(a=(X := 5)):\n    return a\n" + GETX, "5", "6", id="default"
        ),
        pytest.param(
            "Y = [(X := i) + 3 for i in range(3)]\n" + GETX,
            "range(3)",
            "range(4)",
            id="comprehension",
        ),
    ],
)
def test_walrus(project: Project, source: str, old: str, new: str) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": source,
        "tests/test_m.py": (
            "from pkg.m import getx\n\n\ndef test_a():\n    assert getx() in (2, 5)\n"
            + OTHER
        ),
    }
    changes = {"pkg/m.py": source.replace(old, new)}
    assert selects(project, files, changes, "tests/test_m.py::test_a", passed=2)


@pytest.mark.parametrize(
    ("other", "patch", "expected"),
    [
        pytest.param("X = 1\n", "from pkg import other\n\nother.X = 5\n", 5, id="set"),
        pytest.param(
            "X = 1\n", "import pkg.other as o\n\no.X += 4\n", 5, id="augmented"
        ),
        pytest.param(
            "REG = {}\nX = None\n",
            "from pkg import other\n\nother.REG['k'] = 5\n",
            5,
            id="item",
        ),
    ],
)
def test_imported_object_changed(
    project: Project, other: str, patch: str, expected: int
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/other.py": other
        + "\n\ndef getx():\n    return REG['k'] if 'REG' in globals() else X\n",
        "pkg/patch.py": patch,
        "tests/test_m.py": (
            "import pkg.patch\nfrom pkg.other import getx\n\n\n"
            f"def test_a():\n    assert getx() == {expected}\n" + OTHER
        ),
    }
    changes = {"pkg/patch.py": patch.replace("5", "6").replace("4", "5")}
    assert selects(project, files, changes, "tests/test_m.py::test_a", passed=2)
