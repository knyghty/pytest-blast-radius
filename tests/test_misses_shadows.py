"""Changes seen through names a module lacked, class creation and file checks."""

import pytest

from tests.conftest import Project
from tests.test_misses_caches import OTHER
from tests.test_misses_caches import selects

LEN = "\n\ndef len(x):\n    return 0\n"
SIZE = "def size(x):\n    return len(x)\n"


@pytest.mark.parametrize(
    ("files", "changes"),
    [
        pytest.param({"pkg/m.py": SIZE}, {"pkg/m.py": SIZE + LEN}, id="def"),
        pytest.param(
            {
                "pkg/util.py": "def helper():\n    return 1\n",
                "pkg/m.py": "from pkg.util import *\n\n\n" + SIZE,
            },
            {"pkg/util.py": "def helper():\n    return 1\n" + LEN},
            id="star",
        ),
    ],
)
def test_builtin_shadowed(
    project: Project, files: dict[str, str], changes: dict[str, str]
) -> None:
    files = {
        "pkg/__init__.py": "",
        **files,
        "tests/test_m.py": (
            "from pkg.m import size\n\n\ndef test_m():\n    assert size('ab') == 2\n"
            + OTHER
        ),
    }
    assert selects(project, files, changes, "tests/test_m.py::test_m", passed=2)


def test_module_getattr_added(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": "A = 1\n",
        "pkg/use.py": (
            "from pkg import m\n\n\ndef extra():\n    return getattr(m, 'EXTRA', 0)\n"
        ),
        "tests/test_m.py": (
            "from pkg.use import extra\n\n\ndef test_m():\n    assert extra() == 0\n"
            + OTHER
        ),
    }
    changes = {"pkg/m.py": "A = 1\n\n\ndef __getattr__(name):\n    return 5\n"}
    assert selects(project, files, changes, "tests/test_m.py::test_m", passed=2)


@pytest.mark.parametrize(
    ("base", "member"),
    [
        pytest.param(
            "import abc\n\n\nclass Base(abc.ABC):\n    def name(self):\n"
            "        return 'b'\n",
            "\n    @abc.abstractmethod\n    def size(self):\n        pass\n",
            id="abstract",
        ),
        pytest.param(
            "from typing import Protocol\nfrom typing import runtime_checkable\n\n\n"
            "@runtime_checkable\nclass Base(Protocol):\n    def name(self): ...\n",
            "\n    def size(self): ...\n",
            id="protocol",
        ),
    ],
)
def test_member_added_at_class_creation(
    project: Project, base: str, member: str
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/base.py": base,
        "pkg/impl.py": (
            "from pkg.base import Base\n\n\nclass Impl:\n"
            "    def name(self):\n        return 'i'\n\n\n"
            "class Sub(Base):\n    def name(self):\n        return 's'\n\n\n"
            "def check(x):\n    return isinstance(x, Base)\n"
        ),
        "tests/test_i.py": (
            "from pkg import impl\n\n\ndef test_i():\n"
            "    assert impl.Sub().name() == 's'\n"
            "    assert impl.check(impl.Sub())\n"
            "    if 'Protocol' in impl.Base.__mro__[1].__name__:\n"
            "        assert impl.check(impl.Impl())\n" + OTHER
        ),
    }
    changes = {"pkg/base.py": base + member}
    assert selects(project, files, changes, "tests/test_i.py::test_i", passed=2)


def test_os_access(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/flags.py": (
            "import os\n\n\ndef enabled():\n"
            "    return os.access('flags/beta', os.F_OK)\n"
        ),
        "flags/README": "",
        "tests/test_f.py": (
            "from pkg.flags import enabled\n\n\n"
            "def test_f():\n    assert not enabled()\n" + OTHER
        ),
    }
    assert selects(
        project, files, {"flags/beta": ""}, "tests/test_f.py::test_f", passed=2
    )


@pytest.mark.parametrize(
    ("files", "added"),
    [
        # pytest puts the test file's directory first on sys.path.
        pytest.param(
            {
                "helpers.py": "def v():\n    return 1\n",
                "tests/test_h.py": (
                    "from helpers import v\n\n\ndef test_h():\n    assert v() == 1\n"
                    + OTHER
                ),
            },
            "tests/helpers.py",
            id="path",
        ),
        pytest.param(
            {
                "pkg/__init__.py": "",
                "pkg/sub.py": "def v():\n    return 1\n",
                "tests/test_h.py": (
                    "from pkg.sub import v\n\n\ndef test_h():\n    assert v() == 1\n"
                    + OTHER
                ),
            },
            "pkg/sub/__init__.py",
            id="package",
        ),
    ],
)
def test_module_shadowed(project: Project, files: dict[str, str], added: str) -> None:
    changes = {added: "def v():\n    return 2\n"}
    assert selects(project, files, changes, "tests/test_h.py::test_h", passed=2)


def test_test_file_without_tests(project: Project) -> None:
    # It collects nothing here, but pytest, not a framework, loads it.
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": "X = 1\n\n\ndef f():\n    return X\n",
        "tests/test_elsewhere.py": (
            "import sys\n\nfrom pkg import m\n\nif sys.platform == 'nowhere':\n\n"
            "    def test_there():\n        assert m.f()\n"
        ),
        "tests/test_m.py": (
            "from pkg import m\n\n\ndef test_m():\n    assert m.f() == 1\n"
        ),
        "tests/test_x.py": "def test_x():\n    pass\n",
    }
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=2)
    # A top-level call, unlike a binding, changes the whole module.
    project.write({"pkg/m.py": "X = 1\nprint(X)\n\n\ndef f():\n    return X\n"})
    assert project.ran("--blast-radius") == {"tests/test_m.py::test_m"}


def test_module_loaded_by_name(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/base.py": "X = 1\n",
        "pkg/data/__init__.py": "",
        "pkg/data/one.py": "from pkg import base\n\nVALUE = base.X\n",
        "pkg/loader.py": (
            "import importlib\n\n\ndef load(name):\n"
            "    return importlib.import_module(f'pkg.data.{name}')\n"
        ),
        "tests/test_l.py": (
            "from pkg.loader import load\n\n\n"
            "def test_l():\n    assert load('one').VALUE == 1\n"
        ),
        "tests/test_x.py": "def test_x():\n    pass\n",
        # Importing the loader without loading through it doesn't use the module.
        "tests/test_y.py": (
            "from pkg import loader\n\n\ndef test_y():\n    assert loader.load\n"
        ),
    }
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=3)
    # Nothing imports pkg.data.one by name, but loader.py loads it.
    project.write({"pkg/base.py": "X = 1\nprint(X)\n"})
    assert project.ran("--blast-radius") == {"tests/test_l.py::test_l"}


def test_module_read_through_its_package(project: Project) -> None:
    # Importing pkg.data.one anywhere makes it an attribute of pkg.data.
    files = {
        "pkg/__init__.py": "",
        "pkg/data/__init__.py": "",
        "pkg/data/one.py": "VALUE = 1\n",
        "tests/conftest.py": "import pkg.data.one\n",
        "tests/test_p.py": (
            "import pkg\n\n\ndef test_p():\n    assert pkg.data.one.VALUE == 1\n"
        ),
        "tests/test_x.py": "def test_x():\n    pass\n",
    }
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/data/one.py": "VALUE = 2\n"})
    assert "tests/test_p.py::test_p" in project.ran("--blast-radius")


@pytest.mark.parametrize("change", ["VALUE = 1\nprint(VALUE)\n", "VALUE = 2\n"])
@pytest.mark.parametrize(
    "user",
    [
        # As oscar's models load their managers with get_class() while importing.
        pytest.param("from pkg.loader import load\n\nONE = load('one')\n", id="load"),
        pytest.param("from pkg.data import one as ONE\n", id="import"),
    ],
)
def test_module_a_module_loads_by_name(
    project: Project, user: str, change: str
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/data/__init__.py": "",
        "pkg/data/one.py": "VALUE = 1\n",
        "pkg/loader.py": (
            "import importlib\n\n\ndef load(name):\n"
            "    return importlib.import_module(f'pkg.data.{name}')\n"
        ),
        "pkg/user.py": user,
        "tests/test_u.py": (
            "from pkg.user import ONE\n\n\ndef test_u():\n    assert ONE.VALUE == 1\n"
        ),
        "tests/test_x.py": "def test_x():\n    pass\n",
    }
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/data/one.py": change})
    assert project.ran("--blast-radius") == {"tests/test_u.py::test_u"}


@pytest.mark.parametrize("body", ["pass", "y = 1"])
def test_descriptor_held_through_factories(project: Project, body: str) -> None:
    source = (
        f"class D:\n    {body}\n\n\ndef _build():\n    return D()\n\n\n"
        "def make():\n    return _build()\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/d.py": source,
        "pkg/c.py": "from pkg.d import make\n\n\nclass C:\n    x = make()\n",
        "tests/test_c.py": (
            "from pkg.c import C\n\n\ndef test_c():\n    assert C().x != 5\n" + OTHER
        ),
    }
    changes = {
        "pkg/d.py": source.replace(
            f"    {body}\n",
            f"    {body}\n\n    def __get__(self, obj, owner):\n        return 5\n",
            1,
        )
    }
    assert selects(project, files, changes, "tests/test_c.py::test_c", passed=2)


@pytest.mark.parametrize("body", ["pass", "Y = 0"])
@pytest.mark.parametrize(
    ("base", "use"),
    [
        pytest.param(
            "class Base:\n    X = 1\n\n    @classmethod\n    def build(cls):\n"
            "        return cls.X\n",
            "from pkg.child import Child\n\nVALUE = Child.build()\n",
            id="classmethod",
        ),
        pytest.param(
            "REG = {}\n\n\nclass Base:\n    X = 1\n\n"
            "    def __init_subclass__(cls):\n        REG[cls.X] = cls\n",
            "import pkg.child\nfrom pkg.base import REG\n\n"
            "VALUE = 1 if 1 in REG else 2\n",
            id="init_subclass",
        ),
    ],
)
def test_subclass_attribute_read_at_import(
    project: Project, base: str, use: str, body: str
) -> None:
    child = f"from pkg.base import Base\n\n\nclass Child(Base):\n    {body}\n"
    files = {
        "pkg/__init__.py": "",
        "pkg/base.py": base,
        "pkg/child.py": child,
        "pkg/v.py": use,
        "tests/test_v.py": (
            "from pkg.v import VALUE\n\n\ndef test_v():\n    assert VALUE == 1\n"
            + OTHER
        ),
    }
    # Replacing `pass` changes the class; adding beside `Y` adds a member.
    changes = {"pkg/child.py": child.replace("    pass\n", "") + "    X = 2\n"}
    assert selects(project, files, changes, "tests/test_v.py::test_v", passed=2)
