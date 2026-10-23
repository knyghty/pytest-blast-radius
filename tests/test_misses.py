"""Changes that once made selection skip a test that fails, found by review."""

import textwrap
from pathlib import Path

import pytest

from tests.conftest import Project

OTHER = "\n\ndef test_other():\n    pass\n"


def check(
    project: Project, files: dict[str, str], path: str, new: str, expected: set[str]
) -> None:
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=len(expected) + 1)
    project.write({path: new})
    assert expected <= project.ran("--blast-radius")


def test_wrapped_cache(project: Project) -> None:
    source = textwrap.dedent(
        """\
        import functools


        def logged(f):
            @functools.wraps(f)
            def wrapper(*args):
                return f(*args)

            return wrapper


        def base():
            return 1


        @logged
        @functools.cache
        def cached():
            return base()


        class Holder:
            class Inner:
                @property
                @functools.cache
                def value(self):
                    return base()
        """
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/m.py": source,
        "tests/test_m.py": (
            "from pkg import m\n\n\n"
            "def test_a():\n    assert m.cached() == 1\n\n\n"
            "def test_b():\n    assert m.cached() == 1\n\n\n"
            "def test_c():\n    assert m.Holder.Inner().value == 1\n\n\n"
            "def test_d():\n    assert m.Holder.Inner().value == 1\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/m.py",
        source.replace("return 1", "return 2"),
        {f"tests/test_m.py::test_{name}" for name in "abcd"},
    )


def test_getfixturevalue(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/h.py": "def helper():\n    return 1\n",
        "tests/conftest.py": (
            "import pytest\n\nfrom pkg.h import helper\n\n\n"
            "@pytest.fixture(scope='session')\ndef data():\n    return helper()\n"
        ),
        "tests/test_f.py": (
            "def test_a(data):\n    assert data == 1\n\n\n"
            "def test_b(request):\n    assert request.getfixturevalue('data') == 1\n"
            + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/h.py",
        "def helper():\n    return 2\n",
        {"tests/test_f.py::test_a", "tests/test_f.py::test_b"},
    )


def test_names_in_constant_tuples(project: Project) -> None:
    source = (
        "class Conf:\n    a = 1\n    b = 2\n\n\n"
        "def total(c):\n    return sum(getattr(c, name) for name in ('a', 'b'))\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": source,
        "tests/test_c.py": (
            "from pkg.conf import Conf, total\n\n\n"
            "def test_total():\n    assert total(Conf) == 3\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/conf.py",
        source.replace("a = 1", "a = 5"),
        {"tests/test_c.py::test_total"},
    )


def test_attribute_of_module_level_instance(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": "class Conf:\n    timeout = 1\n",
        "pkg/obj.py": "from pkg.conf import Conf\n\nCONF = Conf()\n",
        "tests/test_o.py": (
            "from pkg.obj import CONF\n\n\n"
            "def test_timeout():\n    assert CONF.timeout == 1\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/conf.py",
        "class Conf:\n    timeout = 2\n",
        {"tests/test_o.py::test_timeout"},
    )


def test_optional_module_added(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": (
            "VALUE = 1\ntry:\n    from pkg.local import VALUE\n"
            "except ImportError:\n    pass\n"
        ),
        "tests/test_l.py": (
            "from pkg import conf\n\n\n"
            "def test_value():\n    assert conf.VALUE == 1\n" + OTHER
        ),
    }
    check(
        project, files, "pkg/local.py", "VALUE = 2\n", {"tests/test_l.py::test_value"}
    )


def test_optional_package_installed(
    project: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "optpkg").mkdir()
    (tmp_path / "optpkg" / "__init__.py").write_text("VALUE = 2\n")
    info = tmp_path / "optpkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: optpkg\nVersion: 1.0\n"
    )
    (info / "top_level.txt").write_text("optpkg\n")
    (info / "RECORD").write_text("optpkg/__init__.py,,\n")
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/j.py": (
                "try:\n    from optpkg import VALUE\n"
                "except ImportError:\n    VALUE = 1\n"
            ),
            "tests/test_j.py": (
                "from pkg import j\n\n\ndef test_value():\n    assert j.VALUE == 1\n"
                + OTHER
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=2)
    monkeypatch.syspath_prepend(str(tmp_path))
    project.run("--blast-radius").assert_outcomes(passed=1, failed=1)


def test_insert_after_closing_bracket(project: Project) -> None:
    source = (
        "RESULTS = []\n\n\ndef record(x):\n    RESULTS.append(\n        x,\n    )\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/r.py": source,
        "tests/test_r.py": (
            "from pkg.r import RESULTS, record\n\n\n"
            "def test_record():\n    RESULTS.clear()\n    record(1)\n"
            "    assert RESULTS == [1]\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/r.py",
        source + "    RESULTS.append(x)\n",
        {"tests/test_r.py::test_record"},
    )


def test_changed_exception_clause(project: Project) -> None:
    source = (
        "def parse(s):\n    try:\n        return int(s)\n"
        "    except ValueError:\n        return None\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/p.py": source,
        "tests/test_p.py": (
            "from pkg.p import parse\n\n\n"
            "def test_bad():\n    assert parse('x') is None\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/p.py",
        source.replace("ValueError", "TypeError"),
        {"tests/test_p.py::test_bad"},
    )


DJANGO_CONFTEST = """\
import os

import django
from django.test.utils import setup_test_environment

os.environ["DJANGO_SETTINGS_MODULE"] = "site_.settings"
django.setup()
setup_test_environment()
"""
SETTINGS = """\
INSTALLED_APPS = ["app"]
MIDDLEWARE = []
ROOT_URLCONF = "app.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": ["templates"],
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]
"""
TEMPLATE_TESTS = (
    "from django.template.loader import render_to_string\n\n\n"
    "def test_a():\n    assert render_to_string('x.html') == 'app '\n\n\n"
    "def test_b():\n    assert render_to_string('x.html') == 'app '\n" + OTHER
)
DJANGO = {
    "site_/__init__.py": "",
    "site_/settings.py": SETTINGS,
    "app/__init__.py": "",
    "app/urls.py": "urlpatterns = []\n",
    "app/templates/x.html": "app {{ missing }}",
    "tests/conftest.py": DJANGO_CONFTEST,
    "tests/test_t.py": TEMPLATE_TESTS,
}
BOTH = {"tests/test_t.py::test_a", "tests/test_t.py::test_b"}


def test_setting_read_once(project: Project) -> None:
    new = SETTINGS.replace('"OPTIONS": {}', '"OPTIONS": {"string_if_invalid": "X"}')
    check(project, DJANGO, "site_/settings.py", new, BOTH)


def test_template_shadowed(project: Project) -> None:
    check(project, DJANGO, "templates/x.html", "project", BOTH)


def test_docstring_read_at_runtime(project: Project) -> None:
    source = 'def greet():\n    """Say hi."""\n    return "hi"\n'
    files = {
        "pkg/__init__.py": "",
        "pkg/g.py": source,
        "tests/test_g.py": (
            "from pkg.g import greet\n\n\n"
            "def test_doc():\n    assert greet() == 'hi'\n"
            "    assert greet.__doc__ == 'Say hi.'\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/g.py",
        source.replace("Say hi.", "Say hello."),
        {"tests/test_g.py::test_doc"},
    )


def test_doctest_in_docstring(project: Project) -> None:
    source = (
        'def double(x):\n    """Double x.\n\n    >>> double(2)\n    4\n'
        '    """\n    return x * 2\n'
    )
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/d.py": source,
            "tests/test_d.py": "def test_other():\n    pass\n",
        }
    )
    args = ("--blast-radius", "--doctest-modules", "pkg", "tests")
    project.run(*args).assert_outcomes(passed=2)
    project.write({"pkg/d.py": source.replace("    4\n", "    5\n")})
    assert "pkg/d.py::pkg.d.double" in project.ran(*args)


def test_global_statement_removed(project: Project) -> None:
    source = (
        "COUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT += 1\n"
        "    return COUNT\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/c.py": source,
        "tests/test_c.py": (
            "from pkg.c import bump\n\n\n"
            "def test_bump():\n    assert bump() >= 1\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/c.py",
        source.replace("    global COUNT\n", ""),
        {"tests/test_c.py::test_bump"},
    )


CHILD = """\
{% extends "base.html" %}
{% block content %}
hello
{% endblock %}
"""
CHILD_TESTS = (
    "from django.template.loader import render_to_string\n\n\n"
    "def test_a():\n    assert render_to_string('child.html').split() == ['hello']\n"
    + OTHER
)


def _child(project: Project, new: str) -> None:
    files = {
        **DJANGO,
        "app/templates/base.html": "{% block content %}base{% endblock %}",
        "app/templates/child.html": CHILD,
        "tests/test_t.py": CHILD_TESTS,
    }
    check(project, files, "app/templates/child.html", new, {"tests/test_t.py::test_a"})


def test_insert_into_child_block(project: Project) -> None:
    _child(project, CHILD.replace("hello\n", "hello\nworld\n"))


def test_rename_child_block(project: Project) -> None:
    _child(project, CHILD.replace("block content", "block other"))


def test_verbatim_text(project: Project) -> None:
    template = "{% verbatim %}\nhello\n{% endverbatim %}"
    render = "    assert render_to_string('v.html').split() == ['hello']\n"
    tests = (
        "from django.template.loader import render_to_string\n\n\n"
        f"def test_a():\n{render}\n\ndef test_b():\n{render}" + OTHER
    )
    files = {**DJANGO, "app/templates/v.html": template, "tests/test_t.py": tests}
    check(
        project,
        files,
        "app/templates/v.html",
        template.replace("hello", "bye"),
        {"tests/test_t.py::test_a", "tests/test_t.py::test_b"},
    )


def test_blocktranslate_text(project: Project) -> None:
    template = "{% load i18n %}{% blocktranslate %}\nhello\n{% endblocktranslate %}"
    tests = (
        "from django.template.loader import render_to_string\n\n\n"
        "def test_a():\n    assert render_to_string('i.html').split() == ['hello']\n"
        + OTHER
    )
    files = {**DJANGO, "app/templates/i.html": template, "tests/test_t.py": tests}
    check(
        project,
        files,
        "app/templates/i.html",
        template.replace("hello", "bye"),
        {"tests/test_t.py::test_a"},
    )


URL_TESTS = (
    "from django.urls import resolve\n\n\n"
    "def test_a():\n    assert resolve('/api/x/').url_name == 'x'\n" + OTHER
)
VIEWS = "def x(request):\n    pass\n"


def test_include_held_in_name(project: Project) -> None:
    root = (
        "from django.urls import include, path\n\n"
        "api = include('app.api_urls')\n"
        "urlpatterns = [path('api/', api)]\n"
    )
    files = {
        **DJANGO,
        "app/views.py": VIEWS,
        "app/api_urls.py": (
            "from django.urls import path\n\nfrom app import views\n\n"
            "urlpatterns = [path('x/', views.x, name='x')]\n"
        ),
        "app/urls.py": root,
        "tests/test_t.py": URL_TESTS,
    }
    check(
        project,
        files,
        "app/urls.py",
        root.replace("'api/'", "'v1/'"),
        {"tests/test_t.py::test_a"},
    )


def test_pattern_list_imported_from_url_module(project: Project) -> None:
    extra = (
        "from django.urls import path\n\nfrom app import views\n\n"
        "extra_patterns = [path('x/', views.x, name='x')]\n"
        "urlpatterns = [path('y/', views.x, name='y')]\n"
    )
    files = {
        **DJANGO,
        "app/views.py": VIEWS,
        "app/extra.py": extra,
        "app/urls.py": (
            "from django.urls import include, path\n\n"
            "from app.extra import extra_patterns\n\n"
            "urlpatterns = [\n    path('api/', include(extra_patterns)),\n"
            "    path('sub/', include('app.extra')),\n]\n"
        ),
        "tests/test_t.py": URL_TESTS
        + "\n\ndef test_y():\n    assert resolve('/sub/y/').url_name == 'y'\n",
    }
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=3)
    project.write({"app/extra.py": extra.replace("'x/'", "'z/'")})
    assert "tests/test_t.py::test_a" in project.ran("--blast-radius")


def test_pattern_list_imported_from_plain_module(project: Project) -> None:
    extra = (
        "from django.urls import path\n\nfrom app import views\n\n"
        "extra_patterns = [path('x/', views.x, name='x')]\n"
    )
    files = {
        **DJANGO,
        "app/views.py": VIEWS,
        "app/extra.py": extra,
        "app/urls.py": (
            "from django.urls import include, path\n\n"
            "from app.extra import extra_patterns\n\n"
            "urlpatterns = [path('api/', include(extra_patterns))]\n"
        ),
        "tests/test_t.py": URL_TESTS,
    }
    check(
        project,
        files,
        "app/extra.py",
        extra.replace("'x/'", "'z/'"),
        {"tests/test_t.py::test_a"},
    )


def test_class_attribute_read_through_vars(project: Project) -> None:
    source = (
        "class Colors:\n    RED = 'red'\n    BLUE = 'blue'\n\n\n"
        "def all_colors():\n"
        "    return sorted(v for k, v in vars(Colors).items() if k.isupper())\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/c.py": source,
        "tests/test_c.py": (
            "from pkg.c import all_colors\n\n\n"
            "def test_all():\n    assert all_colors() == ['blue', 'red']\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/c.py",
        source.replace("'red'", "'crimson'"),
        {"tests/test_c.py::test_all"},
    )


def test_attribute_registered_by_set_name(project: Project) -> None:
    source = (
        "class Field:\n    def __set_name__(self, owner, name):\n"
        "        owner.names = [*getattr(owner, 'names', []), name]\n\n\n"
        "class Form:\n    a = Field()\n\n\n"
        "def field_names():\n    return Form.names\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/f.py": source,
        "tests/test_f.py": (
            "from pkg.f import field_names\n\n\n"
            "def test_names():\n    assert field_names() == ['a']\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/f.py",
        source.replace("    a = Field()\n", "    a = Field()\n    b = Field()\n"),
        {"tests/test_f.py::test_names"},
    )


def test_attribute_of_instance_from_factory(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": (
            "class Conf:\n    timeout = 1\n\n\ndef make():\n    return Conf()\n"
        ),
        "pkg/obj.py": "from pkg.conf import make\n\nCONF = make()\n",
        "tests/test_o.py": (
            "from pkg.obj import CONF\n\n\n"
            "def test_timeout():\n    assert CONF.timeout == 1\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/conf.py",
        "class Conf:\n    timeout = 2\n\n\ndef make():\n    return Conf()\n",
        {"tests/test_o.py::test_timeout"},
    )
