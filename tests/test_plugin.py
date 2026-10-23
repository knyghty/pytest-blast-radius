import textwrap

import pytest

from pytest_blast_radius import selection
from pytest_blast_radius import storage
from tests.conftest import PYPROJECT
from tests.conftest import Project

MATHS = {
    "pkg/__init__.py": "",
    "pkg/constants.py": "FACTOR = 2\n",
    "pkg/maths.py": """\
        import functools

        from pkg import constants


        def add(a, b):
            return a + b


        def double(a):
            return a * constants.FACTOR


        @functools.cache
        def one():
            return 1
        """,
    "pkg/text.py": """\
        def shout(text):
            return text.upper()
        """,
    "tests/test_maths.py": """\
        from pkg import maths


        def test_add():
            assert maths.add(1, 2) == 3


        def test_double():
            assert maths.double(2) == 4


        def test_one_first():
            assert maths.one() == 1


        def test_one_second():
            assert maths.one() == 1
        """,
    "tests/test_text.py": """\
        from pkg import text


        def test_shout():
            assert text.shout("a") == "A"
        """,
}
MATHS = {name: textwrap.dedent(source) for name, source in MATHS.items()}
ALL_MATHS = {
    "tests/test_maths.py::test_add",
    "tests/test_maths.py::test_double",
    "tests/test_maths.py::test_one_first",
    "tests/test_maths.py::test_one_second",
}
TEST_COUNT = len(ALL_MATHS) + 1
HANDLERS = "from pkg.registry import HANDLERS\n\nHANDLERS.append({})\n"
TEMPLATES = "django.template.backends.django.DjangoTemplates"


@pytest.fixture
def recorded(project: Project) -> Project:
    project.commit(MATHS)
    result = project.run("--blast-radius")
    result.assert_outcomes(passed=TEST_COUNT)
    return project


def test_first_run_records(project: Project) -> None:
    project.commit(MATHS)
    result = project.run("--blast-radius")
    result.stdout.fnmatch_lines(["blast-radius: running all tests (no recorded run *)"])
    [data] = project.maps()
    assert data["tree"] == project.git("rev-parse", "HEAD^{tree}").strip()
    assert len(data["tests"]) == TEST_COUNT


def test_no_changes_runs_nothing(recorded: Project) -> None:
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(["blast-radius: skipping 5 tests unaffected *"])
    assert result.ret == pytest.ExitCode.OK


def test_function_change(recorded: Project) -> None:
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_add"}


SIGN = {
    "pkg/__init__.py": "",
    "pkg/sign.py": """\
        def sign(x):
            if x > 0:
                return "positive"
            if x < 0:
                return "negative"
            return "zero"
        """,
    "tests/test_sign.py": """\
        from pkg.sign import sign


        def test_positive():
            assert sign(1) == "positive"


        def test_negative():
            assert sign(-1) == "negative"


        def test_zero():
            assert sign(0) == "zero"
        """,
}


def test_branch_change(project: Project) -> None:
    project.commit(SIGN)
    project.run("--blast-radius")
    source = textwrap.dedent(SIGN["pkg/sign.py"])
    source = source.replace('"positive"', '"posi" + "tive"')
    project.write({"pkg/sign.py": source})
    assert project.ran("--blast-radius") == {"tests/test_sign.py::test_positive"}
    source = source.replace("def sign(x):\n", "def sign(x):\n    x = int(x)\n")
    project.write({"pkg/sign.py": source})
    assert len(project.ran("--blast-radius")) == 3  # noqa: PLR2004
    source = source.replace('"negative"', '"nega" + "tive"')
    project.write({"pkg/sign.py": source})
    assert project.ran("--blast-radius") == {"tests/test_sign.py::test_negative"}
    source = source.replace("    if x < 0:", "    if x <= 0:")
    project.write({"pkg/sign.py": source})
    assert project.ran("--blast-radius") == {
        "tests/test_sign.py::test_negative",
        "tests/test_sign.py::test_zero",
    }


def test_cached_function_change(recorded: Project) -> None:
    recorded.write(
        {"pkg/maths.py": MATHS["pkg/maths.py"].replace("return 1", "return 2")}
    )
    assert recorded.ran("--blast-radius") == {
        "tests/test_maths.py::test_one_first",
        "tests/test_maths.py::test_one_second",
    }


def test_imported_constant_change(recorded: Project) -> None:
    recorded.write({"pkg/constants.py": "FACTOR = 3\n"})
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_double"}


def test_comment_change(recorded: Project) -> None:
    recorded.write({"pkg/maths.py": "# A comment.\n" + MATHS["pkg/maths.py"]})
    assert recorded.ran("--blast-radius") == set()


def test_comment_inside_function_with_real_change(recorded: Project) -> None:
    source = MATHS["pkg/maths.py"].replace("a + b", "b + a")
    source = source.replace(
        "return a * constants", "# Scale.\n    return a * constants"
    )
    recorded.write({"pkg/maths.py": source})
    assert recorded.ran("--blast-radius") == {
        "tests/test_maths.py::test_add",
        "tests/test_maths.py::test_double",
    }


def test_insertion_at_end_of_function(recorded: Project) -> None:
    source = MATHS["pkg/text.py"] + "    raise AssertionError\n"
    recorded.write({"pkg/text.py": source})
    assert recorded.ran("--blast-radius") == {"tests/test_text.py::test_shout"}


def test_top_level_insertion(recorded: Project) -> None:
    recorded.write({"pkg/text.py": MATHS["pkg/text.py"] + "LOUD = True\n"})
    assert recorded.ran("--blast-radius") == set()


def test_new_function(recorded: Project) -> None:
    source = MATHS["pkg/text.py"] + "\n\ndef whisper(text):\n    return text.lower()\n"
    recorded.write({"pkg/text.py": source})
    assert recorded.ran("--blast-radius") == set()


def test_test_file_change(recorded: Project) -> None:
    recorded.write({"tests/test_text.py": MATHS["tests/test_text.py"] + "\nX = 1\n"})
    assert recorded.ran("--blast-radius") == set()
    test = MATHS["tests/test_maths.py"].replace("add(1, 2) == 3", "add(2, 1) == 3")
    recorded.write({"tests/test_maths.py": test})
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_add"}
    test = 'import pytest\n\npytestmark = pytest.mark.filterwarnings("ignore")\n' + test
    recorded.write({"tests/test_maths.py": test})
    assert recorded.ran("--blast-radius") == ALL_MATHS


PYTEST_PROJECT = {
    "tests/conftest.py": """\
        import pytest


        @pytest.fixture
        def number():
            return 1


        def pytest_report_header():
            return "header"
        """,
    "tests/test_a.py": "def test_number(number):\n    assert number == 1\n",
    "tests/test_b.py": "def test_other():\n    pass\n",
}
FIXTURE = '@pytest.fixture{}\ndef extra():\n    return "extra"\n'


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("return 1", "return 2", {"tests/test_a.py::test_number"}),
        (
            "\n\n\ndef pytest_report",
            f"\n\n{FIXTURE.format('')}\n\ndef pytest_report",
            set(),
        ),
        (
            "\n\n\ndef pytest_report",
            f"\n\n{FIXTURE.format('(autouse=True)')}\n\ndef pytest_report",
            {"tests/test_a.py::test_number", "tests/test_b.py::test_other"},
        ),
        (
            '"header"',
            '"other"',
            {"tests/test_a.py::test_number", "tests/test_b.py::test_other"},
        ),
    ],
)
def test_conftest_changes(
    project: Project, old: str, new: str, expected: set[str]
) -> None:
    project.commit(PYTEST_PROJECT)
    project.run("--blast-radius")
    conftest = textwrap.dedent(PYTEST_PROJECT["tests/conftest.py"])
    project.write({"tests/conftest.py": conftest.replace(old, new)})
    assert project.ran("--blast-radius") == expected


def test_new_test_file(recorded: Project) -> None:
    recorded.write({"tests/test_new.py": "def test_new():\n    pass\n"})
    assert recorded.ran("--blast-radius") == {"tests/test_new.py::test_new"}


def test_deleted_file(recorded: Project) -> None:
    (recorded.path / "pkg/text.py").unlink()
    result = recorded.run("--blast-radius")
    result.assert_outcomes(errors=1)


def test_collecting_does_not_record(recorded: Project) -> None:
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    recorded.run("--blast-radius", "--collect-only")
    assert len(recorded.maps()) == 1


def test_change_outside_requested_tests(recorded: Project) -> None:
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert recorded.ran("--blast-radius", "tests/test_text.py") == set()
    recorded.write({"tests/test_new.py": "def test_new():\n    pass\n"})
    assert recorded.ran("--blast-radius", "tests/test_new.py") == {
        "tests/test_new.py::test_new"
    }


def test_git_fails_while_selecting(
    recorded: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        msg = "broken"
        raise storage.GitError(msg)

    monkeypatch.setattr(selection, "find_changes", fail)
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(
        ["blast-radius: running all tests (git failed: broken)"]
    )


def test_selection_fails(recorded: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object) -> None:
        msg = "bug"
        raise ValueError(msg)

    monkeypatch.setattr(selection.Selector, "select", fail)
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(
        ["blast-radius: running all tests (selection failed: ValueError('bug'))"]
    )
    assert result.ret == 0


def test_committed_change(recorded: Project) -> None:
    recorded.commit({"pkg/text.py": MATHS["pkg/text.py"].replace("upper", "title")})
    assert recorded.ran("--blast-radius") == {"tests/test_text.py::test_shout"}


def test_class_body_change(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/models.py": """\
                class Model:
                    size = 1

                    def describe(self):
                        return "model"


                class Other:
                    def describe(self):
                        return "other"
                """,
            "pkg/views.py": """\
                from pkg.models import Model


                def size():
                    return Model.size
                """,
            "tests/test_views.py": """\
                from pkg import views


                def test_size():
                    assert views.size() == 1
                """,
            "tests/test_unrelated.py": "def test_unrelated():\n    pass\n",
        }
    )
    project.run("--blast-radius")
    project.write(
        {
            "pkg/models.py": (project.path / "pkg/models.py")
            .read_text()
            .replace("1", "2")
        }
    )
    assert project.ran("--blast-radius") == {"tests/test_views.py::test_size"}


def test_added_method(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/models.py": """\
                import collections


                class Base(collections.UserDict):
                    def name(self):
                        return "base"


                class Model(Base):
                    def describe(self):
                        return "model"
                """,
            "pkg/page.txt": "{{ model.title }}\n",
            "pkg/views.py": """\
                from pathlib import Path

                from pkg.models import Model


                def label():
                    return getattr(Model(), "label", None)


                def other_label():
                    return getattr(object(), "label", None)


                def dynamic():
                    # A name the code builds, which only running Base.name shows.
                    return getattr(Model(), "na" + "me")()


                def render():
                    Model().describe()
                    return (Path(__file__).parent / "page.txt").read_text()
                """,
            "tests/test_views.py": """\
                from pkg import views
                from pkg.models import Model


                def test_describe():
                    assert Model().describe() == "model"


                def test_label():
                    assert views.label() is None


                def test_other_label():
                    assert views.other_label() is None


                def test_dynamic():
                    assert views.dynamic() == "base"


                def test_render():
                    assert views.render()
                """,
        }
    )
    project.run("--blast-radius")
    source = (project.path / "pkg/models.py").read_text()
    for method, expected in [
        ("shout", set()),
        ("label", {"tests/test_views.py::test_label"}),
        ("title", {"tests/test_views.py::test_render"}),
        # Overriding a project base's method reaches what ran the base's.
        ("name", {"tests/test_views.py::test_dynamic"}),
        # Library code could call a library base's method.
        (
            "copy",
            {
                "tests/test_views.py::test_describe",
                "tests/test_views.py::test_dynamic",
                "tests/test_views.py::test_label",
                "tests/test_views.py::test_render",
            },
        ),
    ]:
        added = f"\n    def {method}(self):\n        return '{method}'\n"
        project.write({"pkg/models.py": source + added})
        assert project.ran("--blast-radius") == expected, method
        project.write({"pkg/models.py": source})
        project.run("--blast-radius")


def test_changed_attribute(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/models.py": """\
                import abc


                class Config:
                    limit = 1
                    label = "a"

                    def describe(self):
                        return "config"


                class Child(Config):
                    pass


                class Abstract(abc.ABC):
                    label = "a"

                    def describe(self):
                        return "abstract"
                """,
            "pkg/views.py": """\
                from pkg.models import Abstract
                from pkg.models import Child
                from pkg.models import Config

                DOUBLE = Config.limit * 2


                def label():
                    return Child().label


                def double():
                    return DOUBLE


                def describe():
                    return Config().describe() + Abstract().describe()
                """,
            "tests/test_views.py": """\
                from pkg import views


                def test_label():
                    assert views.label()


                def test_double():
                    assert views.double()


                def test_describe():
                    assert views.describe()
                """,
        }
    )
    project.run("--blast-radius")
    source = (project.path / "pkg/models.py").read_text()
    everything = {
        "tests/test_views.py::test_describe",
        "tests/test_views.py::test_double",
        "tests/test_views.py::test_label",
    }
    for old, new, expected in [
        # Every test that used the class may read an attribute, as through vars(),
        # including through a top-level name assigned from it.
        (
            'label = "a"\n\n    def describe(self):\n        return "config"',
            'label = "b"\n\n    def describe(self):\n        return "config"',
            everything,
        ),
        ("limit = 1", "limit = 2", everything),
        (
            'label = "a"\n\n    def describe(self):\n        return "abstract"',
            'label = "b"\n\n    def describe(self):\n        return "abstract"',
            {"tests/test_views.py::test_describe"},
        ),
    ]:
        project.write({"pkg/models.py": source.replace(old, new)})
        assert project.ran("--blast-radius") == expected, new
        project.write({"pkg/models.py": source})
        project.run("--blast-radius")


def test_removed_name_breaks_importers(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "X = 1\nY = 2\n",
            "pkg/b.py": "from pkg.a import X\nfrom pkg.a import Y\n",
            "pkg/c.py": "from pkg import a\n\nZ = a.X + 1\n",
            # Another X, which doesn't come from pkg.a.
            "pkg/d.py": "import types\n\nW = types.SimpleNamespace(X=1).X\n",
            # No test uses X, but importing pkg.b and pkg.c needs it.
            "tests/test_b.py": """\
                from pkg import b


                def test_y():
                    assert b.Y == 2
                """,
            "tests/test_c.py": """\
                from pkg import c


                def test_c():
                    assert c
                """,
            "tests/test_other.py": (
                "from pkg import d\n\n\ndef test_other():\n    assert d.W\n"
            ),
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/a.py": "Y = 2\n"})
    project.run("--blast-radius").assert_outcomes(errors=2)


@pytest.mark.parametrize(
    "importer",
    [
        "import sys\n\nif sys.version_info > (3,):\n    from pkg.a import X\n",
        "try:\n    from pkg.a import X\nexcept KeyError:\n    pass\n",
        "from pkg.a import *\n\nZ = X\n",
    ],
)
def test_removed_name_breaks_nested_importers(project: Project, importer: str) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "X = 1\nY = 2\n",
            "pkg/b.py": importer + "\nB = 1\n",
            "tests/test_b.py": (
                "from pkg import b\n\n\ndef test_b():\n    assert b.B == 1\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    project.write({"pkg/a.py": "Y = 2\n"})
    project.run("--blast-radius").assert_outcomes(errors=1)


def test_removed_import_and_its_use(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/checks.py": "def check():\n    return 1\n",
            "pkg/m.py": "from pkg.checks import check\n\nCHECKS = [check]\n",
            "pkg/n.py": "from pkg import m\n\nN = 1\n",
            "tests/test_n.py": (
                "from pkg import n\n\n\ndef test_n():\n    assert n.N == 1\n"
            ),
        }
    )
    project.run("--blast-radius")
    # m still loads, so nothing that imports it is affected.
    project.write({"pkg/m.py": "CHECKS = []\n"})
    assert project.ran("--blast-radius") == set()


def test_annotations_of_function_called_on_import(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/deco.py": """\
                def tag(label: str):
                    def wrap(function):
                        function.label = label
                        return function

                    return wrap
                """,
            "pkg/b.py": """\
                from pkg.deco import tag


                @tag("x")
                def g():
                    return 1
                """,
            "tests/test_b.py": """\
                from pkg import b


                def test_g():
                    assert b.g.label == "x"
                """,
        }
    )
    project.run("--blast-radius")
    source = (project.path / "pkg/deco.py").read_text()
    # Calling it returns the same, so what importing pkg.b built is unchanged.
    project.write({"pkg/deco.py": source.replace("label: str", "label: object")})
    assert project.ran("--blast-radius") == set()
    project.write({"pkg/deco.py": source.replace("= label", "= label * 2")})
    assert project.ran("--blast-radius") == {"tests/test_b.py::test_g"}


def test_import_time_call(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/choices.py": """\
                def build():
                    return [1, 2]


                CHOICES = build()
                """,
            "pkg/forms.py": """\
                from pkg import choices


                def valid(value):
                    return value in choices.CHOICES
                """,
            "pkg/other.py": """\
                def other():
                    return 1
                """,
            "tests/test_forms.py": """\
                from pkg import forms


                def test_valid():
                    assert forms.valid(1)
                """,
            "tests/test_other.py": """\
                from pkg import other


                def test_other():
                    assert other.other() == 1
                """,
        }
    )
    project.run("--blast-radius")
    project.write(
        {"pkg/choices.py": "def build():\n    return [2]\n\n\nCHOICES = build()\n"}
    )
    assert project.ran("--blast-radius") == {"tests/test_forms.py::test_valid"}


def test_session_fixture(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/prepare.py": "def prepare():\n    return 1\n",
            "tests/conftest.py": """\
                import pytest

                from pkg.prepare import prepare


                @pytest.fixture(scope="session")
                def prepared():
                    return prepare()
                """,
            "tests/test_fixtures.py": """\
                def test_first(prepared):
                    assert prepared == 1


                def test_second(prepared):
                    assert prepared == 1


                def test_without():
                    pass
                """,
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/prepare.py": "def prepare():\n    return 2\n"})
    assert project.ran("--blast-radius") == {
        "tests/test_fixtures.py::test_first",
        "tests/test_fixtures.py::test_second",
    }


def test_data_files(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/data.txt": "1",
            "pkg/settings.txt": "1",
            "pkg/config.py": """\
                from pathlib import Path

                LEVEL = (Path(__file__).parent / "settings.txt").read_text()


                def level():
                    return LEVEL
                """,
            "tests/test_data.py": """\
                from pathlib import Path


                def test_data():
                    assert Path("pkg/data.txt").read_text() == "1"


                def test_neither():
                    pass
                """,
            "tests/test_config.py": """\
                from pkg import config


                def test_config():
                    assert config.level() == "1"
                """,
            "docs.txt": "",
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/data.txt": "2"})
    assert project.ran("--blast-radius") == {"tests/test_data.py::test_data"}
    project.write({"pkg/data.txt": "1", "pkg/settings.txt": "2"})
    assert project.ran("--blast-radius") == {"tests/test_config.py::test_config"}
    project.write({"pkg/settings.txt": "1", "docs.txt": "Words."})
    assert project.ran("--blast-radius") == set()


def test_module_nothing_imports(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/registry.py": """\
                import importlib

                HANDLERS = []


                def load():
                    importlib.import_module("pkg.handlers")
                    return HANDLERS
                """,
            "pkg/handlers.py": HANDLERS.format(1),
            "tests/test_registry.py": """\
                from pkg import registry


                def test_load():
                    assert registry.load() == [1]
                """,
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/handlers.py": HANDLERS.format(2)})
    result = project.run("--blast-radius")
    # registry.py loads it by name, so the tests that import registry use it.
    result.stdout.fnmatch_lines(["*skipping 0 tests unaffected by 1 changed files*"])
    result.assert_outcomes(failed=1)


def test_attribute_used_through_framework(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/framework.py": """\
                ROUTES = {}


                def dispatch(name):
                    return ROUTES[name]().get()
                """,
            "tests/conftest.py": """\
                import importlib


                def pytest_configure(config):
                    importlib.import_module("pkg.urls")
                """,
            "pkg/base.py": """\
                class View:
                    count = 0

                    def get(self):
                        return self.count
                """,
            "pkg/views.py": """\
                from pkg.base import View


                class Backup(View):
                    count = 10
                """,
            "pkg/urls.py": """\
                from pkg import views
                from pkg.framework import ROUTES

                ROUTES["backup"] = views.Backup
                """,
            "tests/test_views.py": """\
                from pkg.framework import dispatch


                def test_backup():
                    assert dispatch("backup") == 10
                """,
        }
    )
    project.run("--blast-radius")
    project.write(
        {"pkg/views.py": (project.path / "pkg/views.py").read_text()[:-3] + "9\n"}
    )
    result = project.run("--blast-radius")
    result.stdout.fnmatch_lines(["blast-radius: skipping 0 tests unaffected by *"])
    result.assert_outcomes(failed=1)


def test_collection_time_call(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/cases.py": "def cases():\n    return [1, 2]\n",
            "tests/test_cases.py": """\
                from pkg.cases import cases


                def pytest_generate_tests(metafunc):
                    metafunc.parametrize("case", cases())


                def test_case(case):
                    assert case
                """,
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/cases.py": "def cases():\n    return [1, 2, 3]\n"})
    # What collecting a test file runs changes only what it collects.
    assert project.ran("--blast-radius") == {
        f"tests/test_cases.py::test_case[{case}]" for case in (1, 2, 3)
    }


def test_value_read_outside_tests(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/conf.py": "VALUE = 1\n\n\ndef read():\n    return VALUE\n",
            "conftest.py": (
                "from pkg import conf\n\n\n"
                "def pytest_configure(config):\n    conf.read()\n"
            ),
            "tests/test_x.py": "def test_x():\n    pass\n",
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/conf.py": "VALUE = 2\n\n\ndef read():\n    return VALUE\n"})
    result = project.run("--blast-radius")
    result.stdout.fnmatch_lines(["*(read in pkg/conf.py runs outside tests)*"])


def test_annotation_change(project: Project) -> None:
    test = "import pytest\n\n\ndef test_a(tmp_path: {}):\n    pass\n"
    project.commit({"tests/test_a.py": test.format("object"), "tests/test_b.py": test})
    project.run("--blast-radius")
    project.write({"tests/test_a.py": test.format("pytest.TempPathFactory")})
    assert project.ran("--blast-radius") == {"tests/test_a.py::test_a"}


def test_full_run_files(recorded: Project) -> None:
    recorded.write({"tests/conftest.py": ""})
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(["*running all tests (tests/conftest.py changed)*"])
    result.assert_outcomes(passed=TEST_COUNT)


def test_settings_files(project: Project) -> None:
    project.commit(
        {
            **MATHS,
            "pyproject.toml": PYPROJECT + "\n[tool.ruff]\nline-length = 88\n",
            "settings.json": "{}",
            "tests/conftest.py": """\
                from pathlib import Path


                def pytest_configure(config):
                    Path("settings.json").read_text()
                """,
        }
    )
    project.run("--blast-radius")
    project.write({"pyproject.toml": PYPROJECT + "\n[tool.ruff]\nline-length = 99\n"})
    assert project.ran("--blast-radius") == set()
    project.write({"pyproject.toml": PYPROJECT + "addopts = ['-v']\n"})
    result = project.run("--blast-radius")
    result.stdout.fnmatch_lines(["*running all tests (pyproject.toml changed)*"])
    project.write({"settings.json": "[]"})
    result = project.run("--blast-radius")
    result.stdout.fnmatch_lines(["*running all tests (settings.json is read outside *"])


def test_configured_full_run_files(recorded: Project) -> None:
    recorded.commit(
        {
            "pyproject.toml": (
                '[tool.pytest]\npythonpath = ["."]\nblast_radius_full_run = ["*.txt"]\n'
            ),
            "docs.txt": "",
        }
    )
    recorded.run("--blast-radius")
    recorded.write({"docs.txt": "Words."})
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(["*running all tests (docs.txt changed)*"])


def test_also_run(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/models.py": "SIZE = 1\n",
            "tests/test_migrations.py": "def test_migrations():\n    pass\n",
            "pytest.toml": (
                '[pytest]\npythonpath = ["."]\n'
                'blast_radius_also_run = ["models.py: tests/test_migrations.py"]\n'
            ),
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/models.py": "SIZE = 2\n"})
    assert project.ran("--blast-radius") == {
        "tests/test_migrations.py::test_migrations"
    }


def test_also_run_invalid(project: Project) -> None:
    project.commit(
        {
            "tests/test_a.py": "def test_a():\n    pass\n",
            "pytest.toml": '[pytest]\nblast_radius_also_run = ["models.py"]\n',
        }
    )
    project.run("--blast-radius")
    result = project.run("--blast-radius")
    result.stderr.fnmatch_lines(["*blast_radius_also_run entries look like*"])


def test_environment_change(recorded: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = storage.installed()
    monkeypatch.setattr(
        storage, "installed", lambda: {**installed, "unimported-package": "2"}
    )
    assert recorded.ran("--blast-radius") == set()
    monkeypatch.setattr(storage, "installed", lambda: {**installed, "pytest": "0"})
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(
        ["*running all tests (no recorded run with this Python*"]
    )
    result.assert_outcomes(passed=TEST_COUNT)


def test_uncommitted_changes_are_recorded(project: Project) -> None:
    project.commit(MATHS)
    project.write({"pkg/text.py": MATHS["pkg/text.py"] + "\n"})
    project.run("--blast-radius").assert_outcomes(passed=TEST_COUNT)
    assert project.ran("--blast-radius") == set()
    project.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert project.ran("--blast-radius") == {"tests/test_maths.py::test_add"}
    assert len(project.maps()) == 2  # noqa: PLR2004


def test_moved_functions_are_carried(recorded: Project) -> None:
    source = MATHS["pkg/maths.py"].replace(
        "a + b", "(\n        a\n        + b\n        + 0\n    )"
    )
    recorded.write({"pkg/maths.py": source})
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_add"}
    recorded.write({"pkg/maths.py": source.replace("* constants", "* 1 * constants")})
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_double"}


def test_failed_tests_run_again(recorded: Project) -> None:
    recorded.write({"pkg/text.py": MATHS["pkg/text.py"].replace("upper", "lower")})
    assert recorded.ran("--blast-radius") == {"tests/test_text.py::test_shout"}
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert recorded.ran("--blast-radius") == {
        "tests/test_maths.py::test_add",
        "tests/test_text.py::test_shout",
    }
    recorded.write({"pkg/text.py": MATHS["pkg/text.py"]})
    assert recorded.ran("--blast-radius") == {"tests/test_text.py::test_shout"}
    assert recorded.ran("--blast-radius") == set()


def test_reused_failures(recorded: Project) -> None:
    recorded.write({"pkg/text.py": MATHS["pkg/text.py"].replace("upper", "lower")})
    recorded.run("--blast-radius").assert_outcomes(failed=1)
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(
        [
            "*by 1 changed files, reusing 1 failures",
            "*AssertionError*",
            (
                "blast-radius: reused this failure from an earlier run, as nothing "
                "the test uses has changed since"
            ),
        ]
    )
    result.assert_outcomes(passed=1, failed=1)
    # The reused report stays the original one. On CI, pytest's short summary quotes
    # the whole report, note included, so count only the report's own.
    report = recorded.run("--blast-radius").stdout.str().split("short test summary")
    assert report[0].count("reused this") == 1
    xdist = ("-p", "xdist", "-n", "2", "-o", "blast_radius_serial_below=0")
    result = recorded.pytester.runpytest_subprocess("--blast-radius", *xdist)
    result.stdout.fnmatch_lines(["*reusing 1 failures*", "*reused this failure*"])
    for args in (
        ["tests/test_text.py::test_shout"],
        ["tests/test_text.py"],
        ["-o", "blast_radius_reuse_failures=false"],
    ):
        result = recorded.run("--blast-radius", *args)
        assert "reused this" not in result.stdout.str(), args
        result.assert_outcomes(failed=1)


@pytest.mark.parametrize(
    ("path", "fixture"),
    [
        ("tests/test_x.py", "@pytest.fixture\ndef value():\n    return 2\n"),
        (
            "tests/test_x.py",
            "@pytest.fixture(name='value')\ndef other():\n    return 2\n",
        ),
        (
            "tests/conftest.py",
            "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 2\n",
        ),
    ],
)
def test_overriding_fixture(project: Project, path: str, fixture: str) -> None:
    test = "import pytest\n\n\ndef test_a(value):\n    assert value == 1\n"
    project.commit(
        {
            "conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 1\n"
            ),
            "tests/test_x.py": test,
            "tests/test_y.py": "def test_y():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    source = test if path == "tests/test_x.py" else ""
    project.write({path: f"{source}\n\n{fixture}"})
    assert "tests/test_x.py::test_a" in project.ran("--blast-radius")


def test_changed_fixture_in_conftest(project: Project) -> None:
    conftest = "import pytest\n\n\n@pytest.fixture\ndef value():\n    return 1\n"
    project.commit(
        {
            "a/conftest.py": conftest,
            "a/test_a.py": "def test_a(value):\n    assert value == 1\n",
            "a/test_b.py": "def test_b():\n    pass\n",
            "b/test_c.py": "def test_c():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=3)
    project.write({"a/conftest.py": conftest.replace("value", "other")})
    # test_a requested value, which is gone.
    project.run("--blast-radius").assert_outcomes(errors=1)


def test_run_of_named_tests_keeps_what_files_collect(recorded: Project) -> None:
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert recorded.ran("--blast-radius", "tests/test_maths.py::test_add") == {
        "tests/test_maths.py::test_add"
    }
    recorded.write({"pkg/constants.py": "FACTOR = 3\n"})
    assert "tests/test_maths.py::test_double" in recorded.ran("--blast-radius")


@pytest.mark.parametrize(
    ("test", "added"),
    [
        (
            "def test_t():\n    from pkg import feature\n",
            {"pkg/feature.py": ""},
        ),
        (
            (
                "import pathlib\n\n\ndef test_t():\n"
                "    assert pathlib.Path('data.txt').exists()\n"
            ),
            {"data.txt": ""},
        ),
    ],
)
def test_failures_rerun_when_files_are_added(
    project: Project, test: str, added: dict[str, str]
) -> None:
    project.commit({"pkg/__init__.py": "", "tests/test_t.py": test})
    project.run("--blast-radius").assert_outcomes(failed=1)
    project.write(added)
    result = project.run("--blast-radius")
    assert "reused" not in result.stdout.str()
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("header", "field"),
    [
        ("from typing import NamedTuple\n\n\nclass P(NamedTuple):\n", "    y: int\n"),
        ("from typing import TypedDict\n\n\nclass P(TypedDict):\n", "    y: int\n"),
    ],
)
def test_tuple_fields(project: Project, header: str, field: str) -> None:
    source = header + "    x: int\n"
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/p.py": source,
            "tests/test_p.py": (
                "from pkg.p import P\n\n\ndef test_p():\n    assert P(x=1)\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    project.write({"pkg/p.py": source + field})
    assert project.ran("--blast-radius") == {"tests/test_p.py::test_p"}


def test_added_files_can_add_tests(project: Project) -> None:
    project.commit(
        {
            "golden/a.json": "{}",
            "tests/test_golden.py": (
                "import pathlib\n\nimport pytest\n\n\n"
                "@pytest.mark.parametrize(\n"
                "    'path', sorted(p.name for p in pathlib.Path('golden').glob('*'))\n"
                ")\n"
                "def test_golden(path):\n    pass\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    project.write({"golden/b.json": "{}"})
    # The test module lists the directory as it imports, so all its tests run.
    assert project.ran("--blast-radius") == {
        "tests/test_golden.py::test_golden[a.json]",
        "tests/test_golden.py::test_golden[b.json]",
    }


def test_failures_rerun_for_imports_stay_affected(project: Project) -> None:
    views = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/views.py": views,
            "pkg/routes.py": "from pkg import views\n\nROUTES = [views.a, views.b]\n",
            "tests/test_routes.py": (
                "def test_routes():\n"
                "    import pkg.routes\n\n"
                "    assert len(pkg.routes.ROUTES) == 2\n"
            ),
            "tests/test_other.py": "def test_other():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({"pkg/views.py": "def a():\n    return 1\n", "pkg/other.py": ""})
    project.run("--blast-radius").assert_outcomes(failed=1)
    # A run that leaves the failed test out must not keep its failure.
    project.write({"pkg/views.py": views.replace("2", "3")})
    project.run("--blast-radius", "tests/test_other.py")
    result = project.run("--blast-radius")
    assert "reused" not in result.stdout.str()
    result.assert_outcomes(passed=1)


def test_failures_rerun_when_a_module_they_ran_may_now_load(
    project: Project,
) -> None:
    views = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/views.py": views,
            "pkg/routes.py": "from pkg import views\n\nROUTES = [views.a, views.b]\n",
            "tests/test_routes.py": (
                "def test_routes():\n"
                "    import pkg.routes\n\n"
                "    assert len(pkg.routes.ROUTES) == 2\n"
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1)
    project.write({"pkg/views.py": "def a():\n    return 1\n", "pkg/other.py": ""})
    project.run("--blast-radius").assert_outcomes(failed=1)
    # The failed import never got as far as using b, but now succeeds.
    project.write({"pkg/views.py": views.replace("2", "3")})
    result = project.run("--blast-radius")
    assert "reused" not in result.stdout.str()
    result.assert_outcomes(passed=1)


def test_prioritise(project: Project) -> None:
    slow = MATHS["tests/test_maths.py"].replace(
        "def test_add():\n", "def test_add():\n    time.sleep(0.1)\n"
    )
    project.commit({**MATHS, "tests/test_maths.py": "import time\n" + slow})
    project.run("--blast-radius").assert_outcomes(passed=TEST_COUNT)
    failing = MATHS["pkg/text.py"].replace("upper()", "lower()")
    project.write({"pkg/text.py": failing})
    project.run("--blast-radius").assert_outcomes(failed=1)
    swapped = (
        MATHS["pkg/maths.py"]
        .replace("a + b", "b + a")
        .replace("a * constants.FACTOR", "constants.FACTOR * a")
    )
    project.write(
        {
            "pkg/maths.py": swapped,
            "pkg/text.py": failing.replace("lower()", "lower().strip()"),
            "tests/test_new.py": "def test_new():\n    pass\n",
        }
    )

    def order() -> list[str]:
        reprec = project.pytester.inline_run(
            "--blast-radius", "-o", "blast_radius_prioritise=true"
        )
        return [
            report.nodeid.rpartition("::")[2]
            for report in reprec.getreports("pytest_runtest_logreport")
            if report.when == "call"
        ]

    assert order() == ["test_shout", "test_new", "test_double", "test_add"]
    project.write({"pkg/maths.py": MATHS["pkg/maths.py"]})
    # The unchanged failure is reused, and comes last.
    assert order() == ["test_double", "test_add", "test_shout"]
    result = project.pytester.runpytest_subprocess(
        "--blast-radius",
        "-o",
        "blast_radius_prioritise=true",
        "-p",
        "xdist",
        "-n",
        "2",
        "-o",
        "blast_radius_serial_below=0",
        "-x",
    )
    result.stdout.fnmatch_lines(["*likeliest failures first*"])


def test_reused_failure_in_a_subdirectory_with_a_conftest(project: Project) -> None:
    project.commit(
        {
            "a/conftest.py": "",
            "a/test_a.py": "def test_a():\n    assert False\n",
            "b/conftest.py": "",
            "b/test_b.py": "def test_b():\n    pass\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=1, failed=1)
    project.write({"b/test_b.py": "def test_b():\n    assert True\n"})
    project.run("--blast-radius").assert_outcomes(passed=1, failed=1)


def test_reused_failure_between_modules(project: Project) -> None:
    read = "assert __import__('pathlib').Path('data.txt').read_text()"
    project.commit(
        {
            "data.txt": "a",
            "tests/test_x.py": (
                f"def test_1():\n    {read}\n\n\ndef test_2():\n    assert False\n"
            ),
            "tests/test_y.py": f"def test_3():\n    {read}\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2, failed=1)
    project.write({"data.txt": "b"})
    result = project.run("--blast-radius", "-p", "no:randomly")
    result.assert_outcomes(passed=2, failed=1)


def test_unaffected_test_files_are_not_collected(recorded: Project) -> None:
    recorded.write(
        {
            # Git ignores .git, so the marker changes no tree.
            "tests/test_text.py": "import pathlib\n"
            "pathlib.Path('.git/imported').touch()\n"
            + (recorded.path / "tests/test_text.py").read_text()
        }
    )
    recorded.run("--blast-radius")
    marker = recorded.path / ".git/imported"
    marker.unlink()
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_add"}
    assert not marker.exists()


def test_tests_left_out_by_keyword_run_later(project: Project) -> None:
    project.commit(MATHS)
    assert project.ran("--blast-radius", "-k", "test_add") == {
        "tests/test_maths.py::test_add"
    }
    assert project.ran("--blast-radius") == (
        ALL_MATHS - {"tests/test_maths.py::test_add"}
    ) | {"tests/test_text.py::test_shout"}


def test_affected_tests_that_did_not_run_run_next_time(recorded: Project) -> None:
    recorded.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    assert recorded.ran("--blast-radius", "tests/test_text.py") == set()
    assert recorded.ran("--blast-radius") == {"tests/test_maths.py::test_add"}


def test_changes_during_run_are_not_recorded(project: Project) -> None:
    project.commit(
        {
            "tests/test_edit.py": """\
                from pathlib import Path


                def test_edit():
                    Path("tests/test_edit.py").write_text("")
                """
        }
    )
    project.run("--blast-radius-record")
    assert project.maps() == []


def test_interrupted_run_is_not_recorded(project: Project) -> None:
    project.commit({"tests/test_a.py": "def test_a():\n    raise KeyboardInterrupt\n"})
    project.pytester.runpytest_subprocess("--blast-radius")
    assert project.maps() == []


def test_record_merges_into_same_commit(recorded: Project) -> None:
    result = recorded.run("--blast-radius-record", "tests/test_text.py")
    result.stdout.fnmatch_lines(["blast-radius: running all tests, recording"])
    [data] = recorded.maps()
    assert len(data["tests"]) == TEST_COUNT


def test_selected_run_on_recorded_commit_records(project: Project) -> None:
    project.commit(MATHS)
    project.run("--blast-radius-record", "tests/test_text.py")
    assert project.ran("--blast-radius") == ALL_MATHS
    [data] = project.maps()
    assert len(data["tests"]) == TEST_COUNT


def test_untracked_files_are_recorded(recorded: Project) -> None:
    recorded.write({"tests/test_new.py": "def test_new():\n    pass\n"})
    assert recorded.ran("--blast-radius") == {"tests/test_new.py::test_new"}
    assert recorded.ran("--blast-radius") == set()
    (recorded.path / "tests/test_new.py").unlink()
    assert recorded.ran("--blast-radius") == set()
    assert "tests/test_new.py::test_new" not in recorded.maps()[-1]["tests"]


def test_selected_run_on_newer_commit_records(recorded: Project) -> None:
    recorded.commit({"pkg/text.py": MATHS["pkg/text.py"].replace("upper", "title")})
    recorded.run("--blast-radius")
    assert recorded.ran("--blast-radius") == set()
    assert {len(data["tests"]) for data in recorded.maps()} == {TEST_COUNT}


def test_not_a_repository(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(pytester.path.parent))
    pytester.makepyfile(test_a="def test_a():\n    pass\n")
    result = pytester.runpytest("--blast-radius")
    result.stdout.fnmatch_lines(
        ["blast-radius: running all tests: not in a git repository"]
    )
    result.assert_outcomes(passed=1)
    result = pytester.runpytest_subprocess("--blast-radius", "-p", "xdist", "-n", "1")
    result.assert_outcomes(passed=1)


def test_repository_without_commits(project: Project) -> None:
    project.write({"tests/test_a.py": "def test_a():\n    pass\n"})
    project.run("--blast-radius").assert_outcomes(passed=1)
    assert project.ran("--blast-radius") == set()


def test_git_fails_while_snapshotting(
    recorded: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        msg = "broken"
        raise storage.GitError(msg)

    monkeypatch.setattr(storage.Repository, "snapshot", fail)
    result = recorded.run("--blast-radius")
    result.stdout.fnmatch_lines(["blast-radius: running all tests: git failed: broken"])


def test_check(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/maths.py": "def add(a, b):\n    return a + b\n",
            "tests/test_maths.py": """\
                import subprocess
                import sys

                from pkg import maths


                def test_add():
                    assert maths.add(1, 2) == 3


                def test_subprocess():
                    code = "from pkg import maths; assert maths.add(1, 2) == 3"
                    subprocess.run([sys.executable, "-c", code], check=True)


                def test_unaffected():
                    pass
                """,
        }
    )
    result = project.run("--blast-radius-check")
    result.stdout.fnmatch_lines(["*running all tests (no recorded run *)"])
    project.write({"pkg/maths.py": "def add(a, b):\n    return a - b\n"})
    result = project.run("--blast-radius-check")
    result.stdout.fnmatch_lines(
        [
            "blast-radius: running all tests to check selection for 1 changed files",
            "selection would have skipped 2 tests, including 1 failing tests:",
            "- tests/test_maths.py::test_subprocess",
        ]
    )
    project.write({"pkg/maths.py": "def add(a, b):\n    return b + a\n"})
    result = project.run("--blast-radius-check")
    result.stdout.fnmatch_lines(
        ["selection would have run every failing test and skipped 1 tests"]
    )


def test_check_full_run_quiet(recorded: Project) -> None:
    recorded.write({"tests/conftest.py": ""})
    result = recorded.run("--blast-radius-check", "-q")
    result.stdout.fnmatch_lines(
        ["blast-radius: running all tests (tests/conftest.py changed)"]
    )


def test_step_summary(recorded: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = recorded.path.parent / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    recorded.run("--blast-radius")
    assert summary.read_text() == (
        "blast-radius: skipping 5 tests unaffected by 0 changed files\n"
    )


def test_inactive_without_flag(project: Project) -> None:
    project.commit(MATHS)
    result = project.run()
    assert "blast-radius:" not in result.stdout.str()
    assert project.maps() == []


def test_coverage_of_changed_lines(project: Project) -> None:
    project.commit(MATHS)
    # In a subprocess, as coverage measures this run.
    cov = ("--cov=pkg", "--cov-branch", "--blast-radius")
    result = project.pytester.runpytest_subprocess(*cov)
    result.stdout.fnmatch_lines(["*TOTAL*"])
    change = MATHS["pkg/maths.py"].replace("a + b", "b + a")
    project.write({"pkg/maths.py": change})
    result = project.pytester.runpytest_subprocess(*cov)
    result.stdout.fnmatch_lines(
        [
            "*by 1 changed files, reporting coverage of changed lines",
            "blast-radius: tests cover all 1 changed lines",
        ]
    )
    assert "TOTAL" not in result.stdout.str()
    result.assert_outcomes(passed=1)
    branchy = change.replace(
        "    return b + a", "    if a:\n        return a + b\n    return b"
    )
    project.write({"pkg/maths.py": branchy})
    result = project.pytester.runpytest_subprocess(
        *cov, "-p", "xdist", "-n", "2", "-o", "blast_radius_serial_below=0"
    )
    result.stdout.fnmatch_lines(
        [
            "blast-radius: tests cover 1 of 3 changed lines; not:",
            "- pkg/maths.py: 9, partly 7",
        ]
    )
    result.assert_outcomes(passed=1)
    project.write({"pkg/maths.py": MATHS["pkg/maths.py"]})
    result = project.pytester.runpytest_subprocess(
        *cov, "-o", "blast_radius_keep_coverage=true"
    )
    result.stdout.fnmatch_lines(["*TOTAL*"])


def test_coverage_since_patch_base(project: Project) -> None:
    project.commit(MATHS)
    project.git("branch", "start")
    cov = ("--cov=pkg", "--cov-branch", "--blast-radius")
    project.pytester.runpytest_subprocess(*cov)
    project.commit({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    project.pytester.runpytest_subprocess(*cov).assert_outcomes(passed=1)
    project.write({"pkg/text.py": "def shout(text):\n    return str.upper(text)\n"})
    result = project.pytester.runpytest_subprocess(*cov)
    result.stdout.fnmatch_lines(["blast-radius: tests cover all 1 changed lines"])
    result.assert_outcomes(passed=1)
    # The test that ran the line changed before the last run runs again too.
    result = project.pytester.runpytest_subprocess(
        *cov, "--blast-radius-patch-base", "start"
    )
    result.stdout.fnmatch_lines(["blast-radius: tests cover all 2 changed lines"])
    result.assert_outcomes(passed=2)


def test_patch_base_selects_earlier_changes(project: Project) -> None:
    project.commit(MATHS)
    project.git("branch", "start")
    project.run("--blast-radius")
    project.commit({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    project.run("--blast-radius")
    project.write({"pkg/text.py": "def shout(text):\n    return str.upper(text)\n"})
    shout = "tests/test_text.py::test_shout"
    assert project.ran("--blast-radius") == {shout}
    base = ("--blast-radius", "--blast-radius-patch-base", "start")
    assert project.ran(*base) == {shout, "tests/test_maths.py::test_add"}
    result = project.run("--blast-radius", "--blast-radius-patch-base", "nowhere")
    result.stderr.fnmatch_lines(["*--blast-radius-patch-base nowhere: *"])


def test_coverage_of_changed_lines_report(project: Project) -> None:
    project.commit(
        {
            **MATHS,
            # Stands in for pytest-cov, whose own measuring would hide this run's,
            # with data saying line 7 of pkg/maths.py ran.
            "tests/conftest.py": """\
                from pathlib import Path
                from types import SimpleNamespace

                import coverage


                def pytest_addoption(parser, pluginmanager):
                    cov = coverage.Coverage(data_file=None)
                    maths = Path(__file__).parent.parent / "pkg" / "maths.py"
                    cov.get_data().add_lines({str(maths): [7]})
                    stand_in = SimpleNamespace(cov_controller=SimpleNamespace(cov=cov))
                    pluginmanager.register(stand_in, "_cov")


                def pytest_unconfigure(config):
                    plugin = config.pluginmanager.get_plugin("_cov")
                    plugin.cov_controller.cov.get_data().close(force=True)
                """,
        }
    )
    project.run("--blast-radius")
    project.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    result = project.run("--blast-radius")
    result.stdout.fnmatch_lines(
        [
            "*by 1 changed files, reporting coverage of changed lines",
            "blast-radius: tests cover all 1 changed lines",
        ]
    )


def test_xdist(project: Project) -> None:
    project.commit(MATHS)
    xdist = ("-p", "xdist", "-n", "2", "--blast-radius")
    result = project.pytester.runpytest_subprocess(*xdist)
    result.assert_outcomes(passed=TEST_COUNT)
    [data] = project.maps()
    assert len(data["tests"]) == TEST_COUNT
    project.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "b + a")})
    result = project.pytester.runpytest_subprocess(
        *xdist, "-o", "blast_radius_serial_below=0"
    )
    result.assert_outcomes(passed=1)
    assert "created: 2/2 workers" in result.stdout.str()
    project.write({"pkg/maths.py": MATHS["pkg/maths.py"].replace("a + b", "a + b + 0")})
    result = project.pytester.runpytest(*xdist)
    result.stdout.fnmatch_lines(
        ["blast-radius: skipping 4 tests unaffected by 1 changed files, in one process"]
    )
    result.assert_outcomes(passed=1)
    result = project.pytester.runpytest_subprocess(*xdist[:-1])
    result.assert_outcomes(passed=TEST_COUNT)


SHOP = {
    "templates/greeting.html": "Hello",
    "templates/page.html": "{% include 'greeting.html' %}",
    "templates/tagged.html": "{% load shop_tags %}{{ 'a'|shout }}",
    "templates/branch.html": "{% if flag %}\nyes\n{% else %}\nno\n{% endif %}\n",
    "shop/templatetags/__init__.py": "",
    "shop/labels.py": "LABELS = []\nLABELS.append('a')\n",
    "shop/templatetags/shop_tags.py": """\
        from django import template

        from shop import labels

        register = template.Library()


        @register.filter
        def shout(value):
            return value.upper()
        """,
    "shop/templatetags/everywhere.py": """\
        from django import template

        register = template.Library()
        """,
    "shop/management/__init__.py": "",
    "shop/management/commands/__init__.py": "",
    "shop/management/commands/hello.py": """\
        from django.core.management.base import BaseCommand

        GREETING = "hi"


        class Command(BaseCommand):
            def handle(self, *args, **options):
                self.stdout.write(GREETING)
        """,
    "shop/__init__.py": "",
    "shop/models.py": """\
        from django.db import models


        class Product(models.Model):
            name = models.CharField(max_length=10)
        """,
    "shop/forms.py": """\
        from django import forms


        class ProductForm(forms.Form):
            name = forms.CharField(max_length=5)
        """,
    "shop/views.py": """\
        from django.views.generic import RedirectView


        class Go(RedirectView):
            url = "/a"


        def hello(request):
            pass
        """,
    "shop/urls.py": """\
        from django.urls import include
        from django.urls import path

        from shop import views

        urlpatterns = [
            path("go", views.Go.as_view(), name="go"),
            path("hello", views.hello),
            path("items/", include("shop.item_urls")),
            path("extra/", include([path("x", views.Go.as_view(), name="x")])),
            path("accounts/", include("django.contrib.auth.urls")),
        ]
        """,
    "shop/item_urls.py": """\
        from django.urls import path

        from shop import views

        app_name = "items"
        urlpatterns = [path("list", views.Go.as_view(), name="list")]
        """,
    "shop/admin.py": """\
        from django.contrib import admin

        from shop.models import Product

        admin.site.register(Product)
        """,
    "shop/settings.py": f"""\
        import os

        ADMIN = not os.environ.get("SHOP_WITHOUT_ADMIN")
        INSTALLED_APPS = [
            *(["django.contrib.admin"] if ADMIN else []),
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.messages",
            "django.contrib.sessions",
            "shop",
        ]
        MIDDLEWARE = []
        ROOT_URLCONF = "shop.urls"
        TEMPLATES = [
            {{
                "BACKEND": "{TEMPLATES}",
                "DIRS": ["templates"],
                "OPTIONS": {{"builtins": ["shop.templatetags.everywhere"]}},
            }}
        ]
        GREETING = "hi"
        LOUD_GREETING = GREETING.upper()
        """,
    "tests/conftest.py": """\
        import os

        import django
        from django.test.utils import setup_test_environment

        os.environ["DJANGO_SETTINGS_MODULE"] = "shop.settings"
        django.setup()
        setup_test_environment()
        if not os.environ.get("SHOP_WITHOUT_ADMIN"):
            __import__("shop.admin")
        """,
    # The tests reach the classes only through Django, never by name.
    "tests/test_shop.py": """\
        import asyncio
        import importlib
        import io

        from django.apps import apps
        from django.core.management import call_command
        from django.template.loader import render_to_string
        from django.test import AsyncClient
        from django.test import Client
        from django.test import RequestFactory
        from django.urls import NoReverseMatch
        from django.urls import resolve
        from django.urls import reverse


        def test_page():
            assert render_to_string("page.html") == "Hello"


        def test_model():
            model = apps.get_model("shop", "product")
            assert model(name="x").name == "x"
            assert model.objects.all().model is model


        def test_form():
            form_class = vars(importlib.import_module("shop.forms"))["ProductForm"]
            assert not form_class({"name": "abcdef"}).is_valid()


        def test_view():
            assert Client().get("/go").url == "/a"
            assert Client().get("/extra/x").url == "/a"
            assert resolve("/accounts/login/").url_name == "login"


        def test_async():
            assert asyncio.run(AsyncClient().get("/go")).url == "/a"


        def test_options():
            view = vars(importlib.import_module("shop.views"))["Go"]()
            view.setup(RequestFactory().get("/"))
            assert view.url == "/a"


        def test_missing_namespace():
            try:
                reverse("nothing:x")
            except NoReverseMatch:
                pass


        def test_reverse():
            assert reverse("items:list") == "/items/list"
            assert reverse("go") == "/go"
            assert reverse(importlib.import_module("shop.views").hello) == "/hello"


        def test_tag():
            assert render_to_string("tagged.html") == "A"


        def test_setting():
            from django.conf import settings

            assert settings.LOUD_GREETING == "HI"
            assert settings.LOUD_GREETING == "HI"


        def test_branch_yes():
            assert "yes" in render_to_string("branch.html", {"flag": True})


        def test_branch_no():
            assert "no" in render_to_string("branch.html", {"flag": False})


        def test_command():
            output = io.StringIO()
            call_command("hello", stdout=output)
            assert output.getvalue() == "hi\\n"


        def test_other():
            pass
        """,
}
URL_USERS = "test_view test_async test_reverse test_missing_namespace"
BRANCHES = "test_branch_yes test_branch_no"


@pytest.mark.parametrize(
    ("path", "old", "new", "test", "admin"),
    [
        ("templates/greeting.html", "Hello", "Hi", "test_page", False),
        # Only renders that took the changed branch.
        ("templates/branch.html", "yes", "YES", "test_branch_yes", False),
        ("templates/branch.html", "{% else %}", "{% elif 1 %}", BRANCHES, False),
        ("shop/models.py", "max_length=10", "max_length=11", "test_model", True),
        ("shop/forms.py", "max_length=5", "max_length=6", "test_form", True),
        ("shop/views.py", '"/a"', '"/b"', "test_view test_async test_options", True),
        # Only Django's calls of the method it overrides find it.
        (
            "shop/views.py",
            '    url = "/a"\n',
            (
                '    url = "/a"\n\n    def get_redirect_url(self, *args, **kwargs):\n'
                '        return "/a"\n'
            ),
            "test_view test_async",
            True,
        ),
        ("shop/item_urls.py", 'path("list"', 'path("all"', "test_reverse", True),
        # A changed include may change namespaces.
        (
            "shop/urls.py",
            '"items/"',
            '"things/"',
            "test_reverse test_missing_namespace",
            True,
        ),
        # The include holding x changes, and reversing a view rather than a name
        # depends on every pattern.
        (
            "shop/urls.py",
            'path("x"',
            'path("y"',
            "test_view test_reverse test_missing_namespace",
            True,
        ),
        # A URLconf that may not load, or changes more than patterns, may change
        # every URL.
        # test_command names the command "hello" in a string.
        (
            "shop/views.py",
            "def hello(request):\n    pass\n",
            "",
            f"{URL_USERS} test_command",
            True,
        ),
        (
            "shop/urls.py",
            '    path("hello", views.hello),',
            '    path("hello", views.hello),\n    path("bye", views.bye),',
            URL_USERS,
            True,
        ),
        (
            "shop/urls.py",
            "urlpatterns = [",
            'handler404 = "shop.views.hello"\n\nurlpatterns = [',
            URL_USERS,
            True,
        ),
        (
            "shop/templatetags/shop_tags.py",
            "@register.filter\ndef shout",
            (
                "@register.filter\ndef whisper(value):\n    return value\n\n\n"
                "@register.filter\ndef shout"
            ),
            "test_tag",
            True,
        ),
        ("shop/management/commands/hello.py", '"hi"', '"hello"', "test_command", True),
        # The tests that read a changed setting, or one computed from it.
        (
            "shop/settings.py",
            'GREETING = "hi"',
            'GREETING = "hey"',
            "test_setting",
            True,
        ),
        # Only Django loads tag libraries, so what one imports reaches tests through
        # the templates that load it. test_page rendered first, so it imported
        # every library.
        ("shop/labels.py", "'a'", "'b'", "test_page test_tag", True),
        (
            "shop/templatetags/everywhere.py",
            "Library()\n",
            "Library()\n\n\n@register.filter\ndef shout(value):\n    return value\n",
            # Django's system checks look for the library while test_command runs.
            f"test_page test_tag {BRANCHES} test_command",
            True,
        ),
    ],
)
def test_django(  # noqa: PLR0913
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    *,
    path: str,
    old: str,
    new: str,
    test: str,
    admin: bool,
) -> None:
    if not admin:
        monkeypatch.setenv("SHOP_WITHOUT_ADMIN", "1")
    project.commit(SHOP)
    project.run("--blast-radius").assert_outcomes(passed=14)
    project.write({path: textwrap.dedent(SHOP[path]).replace(old, new)})
    assert project.ran("--blast-radius") == {
        f"tests/test_shop.py::{name}" for name in test.split()
    }
