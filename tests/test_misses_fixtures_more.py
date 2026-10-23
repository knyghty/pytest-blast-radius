"""Fixtures that reach names through strings or constants."""

import pytest

from tests.conftest import Project
from tests.test_misses_caches import OTHER
from tests.test_misses_caches import selects


@pytest.mark.parametrize(
    "fixture",
    [
        pytest.param(
            "import pytest\n\n\n@pytest.fixture\n"
            "def patched(monkeypatch):\n    monkeypatch.setattr('pkg.m.X', 2)\n",
            id="dotted",
        ),
        pytest.param(
            "import pytest\n\nfrom pkg import m\n\n\n@pytest.fixture\n"
            "def patched(monkeypatch):\n    monkeypatch.setattr(m, 'X', 2)\n",
            id="attribute",
        ),
    ],
)
def test_monkeypatched_name_removed(project: Project, fixture: str) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": "X = 1\n",
        "tests/conftest.py": fixture,
        "tests/test_a.py": "def test_a(patched):\n    pass\n" + OTHER,
    }
    changes = {"pkg/m.py": "Y = 1\n"}
    assert selects(project, files, changes, "tests/test_a.py::test_a", passed=2)


@pytest.mark.parametrize(
    ("conftest", "old", "new", "test"),
    [
        pytest.param(
            "import pytest\n\nNAME = 'res'\n\n\n@pytest.fixture(name=NAME)\n"
            "def make():\n    return 1\n",
            "'res'",
            "'other'",
            "def test_a(res):\n    assert res == 1\n",
            id="name",
        ),
        pytest.param(
            "import pytest\n\nAUTO = False\n\n\n@pytest.fixture(autouse=AUTO)\n"
            "def boom():\n    raise ValueError\n",
            "= False",
            "= True",
            "def test_a():\n    pass\n",
            id="autouse",
        ),
    ],
)
def test_fixture_decorator_from_constant(
    project: Project, conftest: str, old: str, new: str, test: str
) -> None:
    files = {"tests/conftest.py": conftest, "tests/test_a.py": test + OTHER}
    changes = {"tests/conftest.py": conftest.replace(old, new)}
    assert selects(project, files, changes, "tests/test_a.py::test_a", passed=2)


def test_usefixtures_from_constant(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/names.py": "FIXTURES = ['ok']\n",
        "tests/conftest.py": (
            "import pytest\n\n\n@pytest.fixture\ndef ok():\n    pass\n\n\n"
            "@pytest.fixture\ndef boom():\n    raise ValueError\n"
        ),
        "tests/test_a.py": (
            "import pytest\n\nfrom pkg.names import FIXTURES\n\n\n"
            "@pytest.mark.usefixtures(*FIXTURES)\ndef test_a():\n    pass\n" + OTHER
        ),
    }
    changes = {"pkg/names.py": "FIXTURES = ['boom']\n"}
    assert selects(project, files, changes, "tests/test_a.py::test_a", passed=2)
