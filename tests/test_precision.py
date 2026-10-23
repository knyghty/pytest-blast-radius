"""Changes that reach only what can see them, from replays of real projects."""

import pytest

from pytest_blast_radius import selection
from tests.conftest import Project


def test_import_added_to_a_module_code_elsewhere_names(project: Project) -> None:
    # A string naming the bound name counts only from code that reaches the module.
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "import json\n",
            "pkg/other.py": "def names():\n    return ['os']\n\n\nVALUE = names()\n",
            "tests/test_m.py": "from pkg import m\n\n\ndef test_m():\n    assert m\n",
            "tests/test_o.py": (
                "from pkg.other import VALUE\n\n\ndef test_o():\n    assert VALUE\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/m.py": "import json\nimport os\n"})
    assert project.ran("--blast-radius") == set()


CONFTEST = (
    "import pytest\n\nfrom pkg import m\n\n\n"
    "@pytest.fixture\ndef value():\n    return m.f()\n"
)


@pytest.mark.parametrize(
    ("conftest", "full"),
    [
        pytest.param(CONFTEST, False, id="fixtures"),
        # What a call or a hook does may reach tests anywhere.
        pytest.param(CONFTEST + "\nm.f()\n", True, id="call"),
        pytest.param(
            CONFTEST + "\n\ndef pytest_configure(config):\n    pass\n", True, id="hook"
        ),
    ],
)
def test_conftest_importing_a_changed_module(
    project: Project, conftest: str, full: bool
) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "def f():\n    return 1\n",
            "tests/sub/conftest.py": conftest,
            "tests/sub/test_s.py": "def test_s(value):\n    assert value == 1\n",
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    # A top-level call, unlike a binding, changes the whole module.
    project.write({"pkg/m.py": "def f():\n    return 1\n\n\nprint(f())\n"})
    expected = {"tests/sub/test_s.py::test_s"}
    if full:
        expected.add("tests/test_x.py::test_x")
    assert project.ran("--blast-radius") == expected


def test_reader_compiled_from_a_template(project: Project) -> None:
    # As Jinja compiles a template to Python under the template's path.
    loader = (
        "import types\nfrom pathlib import Path\n\nfrom pkg import m\n\n"
        "path = str(Path(__file__).parent / 't.tmpl')\n"
        "module = compile('def f():\\n    return m.X\\n', path, 'exec')\n"
        "f = types.FunctionType(module.co_consts[0], {'m': m})\n"
        "VALUE = f()\n"
    )
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "X = 1\n",
            "pkg/t.tmpl": "{% block x %}{% endblock %}\n",
            "pkg/t.py": loader,
            "tests/test_t.py": (
                "from pkg.t import VALUE\n\n\ndef test_t():\n    assert VALUE == 1\n"
            ),
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/m.py": "X = 2\n"})
    assert project.ran("--blast-radius") == {"tests/test_t.py::test_t"}


@pytest.mark.parametrize(
    ("extra", "full"),
    [pytest.param("", False, id="defines"), pytest.param("f()\n", True, id="call")],
)
def test_module_a_library_loads(project: Project, extra: str, full: bool) -> None:
    # Nothing imports pkg.ext but pytest, as Sphinx loads an extension.
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/base.py": "X = 1\n",
            "pkg/ext.py": "from pkg import base\n\n\ndef f():\n    return base.X\n"
            + extra,
            "tests/test_e.py": (
                "import pytest\n\next = pytest.importorskip('pkg.ext')\n\n\n"
                "def test_e():\n    assert ext.f() == 1\n"
            ),
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/base.py": "X = 1\nprint(X)\n"})
    expected = {"tests/test_e.py::test_e"}
    if full:
        expected.add("tests/test_x.py::test_x")
    assert project.ran("--blast-radius") == expected


def test_defines_only(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "class A:\n    pass\n",
            "tests/test_x.py": "import pkg.m\n\n\ndef test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    [data] = project.maps()
    selector = selection.Selector(data, [], [])
    path = "pkg/m.py"
    assert selector.defines_only(path, "if X:\n    Y = 1\nclass A:\n    pass\n")
    assert not selector.defines_only(path, "def (")
    assert not selector.defines_only(path, "@dataclass\nclass A:\n    pass\n")
    # A class the recording didn't see as plain may register itself.
    assert not selector.defines_only(path, "class B:\n    pass\n")


def test_method_pytest_calls_while_collecting(project: Project) -> None:
    # Pytest reads attributes such as `__test__` of a test module's objects, as
    # Sphinx's Config.__getattr__ answers.
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/config.py": (
                "class Config:\n    def __getattr__(self, name):\n"
                "        raise AttributeError(name)\n"
            ),
            "tests/test_c.py": (
                "from pkg.config import Config\n\nconfig = Config()\n\n\n"
                "def test_c():\n    pass\n"
            ),
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write(
        {
            "pkg/config.py": (
                "class Config:\n    def __getattr__(self, name):\n"
                "        raise AttributeError(f'no {name}')\n"
            )
        }
    )
    assert project.ran("--blast-radius") == {"tests/test_c.py::test_c"}


def test_class_built_from_a_changed_class(project: Project) -> None:
    # B names A, but what B's method computes as modules load doesn't read A.
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "class A:\n    pass\n",
            "pkg/b.py": (
                "from pkg.a import A\n\n\nclass B:\n    model = A\n\n"
                "    def build(self):\n        return 1\n\n\nVALUE = B().build()\n"
            ),
            "tests/test_b.py": (
                "from pkg.b import VALUE\n\n\ndef test_b():\n    assert VALUE == 1\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    project.write({"pkg/a.py": "class A:\n    X = 1\n"})
    assert project.ran("--blast-radius") == set()
