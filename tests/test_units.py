import difflib
import functools
import importlib.util
import os
import sys
import sysconfig
import textwrap
import threading
import types
from pathlib import Path
from typing import Any

import coverage
import pytest

from pytest_blast_radius import migrations
from pytest_blast_radius import names
from pytest_blast_radius import patch
from pytest_blast_radius import plugin
from pytest_blast_radius import recording
from pytest_blast_radius import selection
from pytest_blast_radius import storage
from pytest_blast_radius import templates
from pytest_blast_radius import urlconfs
from pytest_blast_radius.selection import Hunk


def test_parse_diff_skips_quoted_paths() -> None:
    diff = 'diff --git "a/\\303\\251.py" "b/\\303\\251.py"\n--- "a/\\303\\251.py"\n'
    diff += '+++ "b/\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert selection.parse_diff(diff) == {}


def test_parse_diff_keeps_added_lines() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -2,0 +3,2 @@\n+a\n+b\n"
    assert selection.parse_diff(diff) == {
        "x.py": [Hunk(2, 0, ["a", "b"], new_start=3, new_count=2)]
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("x = 1\n", "x = 1  # One.\n"),
        (
            "if TYPE_CHECKING:\n    import a\n",
            "if TYPE_CHECKING:\n    import b\n",
        ),
        (
            "if typing.TYPE_CHECKING:\n    import a\n",
            "if typing.TYPE_CHECKING:\n    import b\n",
        ),
        ("def f():\n    x: int = 1\n", "def f():\n    x: str = 1\n"),
        ("async def f():\n    x: int\n", "async def f():\n    x: str\n"),
    ],
)
def test_equivalent(old: str, new: str) -> None:
    assert selection.equivalent(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("x = 1\n", "x = 2\n"),
        ("class A:\n    x: int\n", "class A:\n    x: str\n"),
        (
            "def f():\n    class A:\n        x: int\n",
            "def f():\n    class A:\n        x: str\n",
        ),
        ("def f():\n    self.x: int = 1\n", "def f():\n    self.x: str = 1\n"),
        ("if x:\n    import a\n", "if x:\n    import b\n"),
        ("x = (\n", "x = 1\n"),
    ],
)
def test_not_equivalent(old: str, new: str) -> None:
    assert not selection.equivalent(old, new)


SOURCE = textwrap.dedent(
    """\
    import os


    def outer():
        # A comment.
        def inner():
            return 1

        return inner


    class Model:
        size = 1

        def method(self):
            return 2
    """
)


@pytest.mark.parametrize(
    ("hunks", "owners", "top_lines"),
    [
        ([Hunk(5, 1)], {("outer", 4): None}, set()),
        ([Hunk(7, 1)], {("outer.<locals>.inner", 6): {7}}, set()),
        ([Hunk(9, 1)], {("outer", 4): {9}}, set()),
        ([Hunk(13, 1)], {}, {13}),
        ([Hunk(16, 1)], {("Model.method", 15): {16}}, set()),
        ([Hunk(16, 0, ["        return 3"])], {("Model.method", 15): {16}}, set()),
        ([Hunk(15, 0, ["        x = 3"])], {("Model.method", 15): None}, set()),
        ([Hunk(16, 0, ["    other = 1"])], {}, set()),
        ([Hunk(16, 0, [""])], {("Model.method", 15): {16}}, set()),
        ([Hunk(1, 1)], {}, {1}),
        ([Hunk(15, 1)], {("Model.method", 15): None}, {15}),
    ],
)
def test_find_owners(
    hunks: list[Hunk], owners: selection.Owners, top_lines: set[int]
) -> None:
    assert selection.find_owners(SOURCE, "x.py", hunks) == (owners, top_lines)


BRANCHES = textwrap.dedent(
    """\
    def f(x):
        if x:
            a = 1
            b = (
                2
            )
        else:
            a = 3
        for i in range(x):
            pass
        return a
    """
)


@pytest.mark.parametrize(
    ("hunks", "reached"),
    [
        ([Hunk(3, 1)], {3}),
        ([Hunk(5, 1)], {4, 5, 6}),
        ([Hunk(7, 1)], {2}),
        ([Hunk(2, 1)], {2}),
        ([Hunk(9, 1)], {9}),
        ([Hunk(6, 0, ["        c = 4"])], {4, 5, 6}),
        ([Hunk(6, 0, ["    c = 4"])], {2}),
        ([Hunk(2, 0, ["        c = 4"])], {2}),
        ([Hunk(10, 0, ["    c = 4"])], {9}),
    ],
)
def test_find_owners_reaches_statements(hunks: list[Hunk], reached: set[int]) -> None:
    assert selection.find_owners(BRANCHES, "x.py", hunks) == (
        {("f", 1): reached},
        set(),
    )


@pytest.mark.parametrize(
    ("hunks", "moves"),
    [
        (None, {1: 1, 5: 5}),
        ([Hunk(2, 1, new_start=2, new_count=3)], {1: 1, 2: None, 3: 5}),
        ([Hunk(2, 0, new_start=3, new_count=2)], {2: 2, 3: 5}),
        (
            [
                Hunk(5, 2, new_start=5, new_count=0),
                Hunk(1, 0, new_start=2, new_count=1),
            ],
            {1: 1, 2: 3, 5: None, 6: None, 7: 6},
        ),
    ],
)
def test_line_moves(hunks: list[Hunk] | None, moves: dict[int, int | None]) -> None:
    move = selection.line_moves(hunks)
    assert {line: move(line) for line in moves} == moves


def hunks_between(old: str, new: str) -> list[Hunk]:
    """Return the hunks `git diff -U0` would show."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    return [
        Hunk(
            i1 + 1 if i2 > i1 else i1,
            i2 - i1,
            new_lines[j1:j2],
            new_start=j1 + 1 if j2 > j1 else j1,
            new_count=j2 - j1,
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def changes(  # noqa: PLR0913
    values: str = "",
    definitions: str = "",
    *,
    used: str = "",
    bindings: str = "",
    methods: str = "",
    attributes: str = "",
    removed: str = "",
) -> names.Changes:
    return names.Changes(
        frozenset(values.split()),
        frozenset(definitions.split()),
        frozenset(used.split()),
        frozenset(bindings.split()),
        frozenset(
            (cls, name, kind)
            for kind, members in (("method", methods), ("attribute", attributes))
            for cls, _, name in (member.partition(".") for member in members.split())
        ),
        frozenset(removed.split()),
    )


CLASS = "class A:\n    x = 1\n\n    def f(self):\n        pass\n"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("X = 1\n", "X = 2\n", changes("X")),
        ("X, Y = 1, 2\n", "X, Y = 1, 3\n", changes("X Y")),
        ("class A:\n    x = 1\n", "class A:\n    x = 2\n", changes(attributes="A.x")),
        (
            "class A:\n    x = 1\n    y = x\n",
            "class A:\n    x = 2\n    y = x\n",
            changes(definitions="A"),
        ),
        (
            "@d\nclass A:\n    x = 1\n",
            "@d\nclass A:\n    x = 2\n",
            changes(definitions="A"),
        ),
        (
            "def f(a=1):\n    pass\n",
            "def f(a=2):\n    pass\n",
            changes(definitions="f"),
        ),
        ("X = 1\n", "X = 1\n\n\ndef f():\n    pass\n", changes(definitions="f")),
        (
            "from loaded import b\n",
            "from loaded import b as c\n",
            changes(bindings="b c", removed="b"),
        ),
        (
            "import loaded\n",
            "import loaded as other\n",
            changes(bindings="loaded other", removed="loaded"),
        ),
        ("import loaded\n", "import loaded.sub\n", None),
        ('"""Doc."""\n', '"""Docs."""\n', changes("__doc__")),
        ("X = 1\n", "X = 1\n# A comment.\n", changes()),
        (
            "if TYPE_CHECKING:\n    import a\n",
            "if TYPE_CHECKING:\n    import b\n",
            changes(),
        ),
        ("x.y = 1\n", "x.y = 2\n", changes("x y")),
        ("X[0] = 1\n", "X[0] = 2\n", changes("X")),
        (
            "@d(1)\ndef f():\n    pass\n",
            "@d(2)\ndef f():\n    pass\n",
            None,
        ),
        (
            "@pytest.mark.a(1)\ndef f():\n    pass\n",
            "@pytest.mark.a(2)\ndef f():\n    pass\n",
            changes(definitions="f"),
        ),
        (
            "class A(B):\n    x = 1\n",
            "class A(C):\n    x = 1\n",
            changes(definitions="A", used="B C"),
        ),
        ("X = 1\n", "X = 1\n\n\nclass A:\n    pass\n", changes(definitions="A")),
        (
            "X = 1\n",
            "X = 1\n\n\n@dataclass\nclass A:\n    x: int\n",
            changes(definitions="A"),
        ),
        (
            "X = 1\n",
            (
                "X = 1\n\n\n@pytest.mark.parametrize('s', [Status.NEW])\n"
                "def test_s(s):\n    pass\n"
            ),
            changes(definitions="test_s"),
        ),
        (
            "X = 1\n",
            "X = 1\n\n\n@register.filter(name=NAME)\ndef f():\n    pass\n",
            None,
        ),
        ("class A:\n    pass\n", "X = 1\n", changes("X", "A", removed="A")),
        ("@d\ndef f():\n    pass\n", "@e\ndef f():\n    pass\n", None),
        ("X = 1\n", "X = 1\n\n\n@d\ndef f():\n    pass\n", None),
        ("X = 1\n", "X = 1\nregister(X)\n", None),
        ("X = 1\n", "X = 1\nimport unknown\n", None),
        ("X = 1\n", "X = 1\nfrom a import *\n", None),
        ("X = 1\n", "X = (\n", None),
        ("a, *b = 1, 2\n", "a, *b = 1, 3\n", changes("a b")),
        ("f().x = 1\n", "f().x = 2\n", None),
        (CLASS, CLASS + "\n    def g(self):\n        pass\n", changes(methods="A.g")),
        (
            CLASS,
            CLASS + "\n    # G.\n    @property\n    def g(self):\n        pass\n",
            changes(methods="A.g"),
        ),
        (
            CLASS,
            CLASS + "\n    @d\n    def g(self):\n        pass\n",
            changes(definitions="A"),
        ),
        (CLASS + "\n    def g(self):\n        pass\n", CLASS, changes(definitions="A")),
        (CLASS, CLASS + "    y = 2\n", changes(attributes="A.y")),
        (
            CLASS,
            CLASS + "\n    if TYPE_CHECKING:\n        def g(self): ...\n",
            changes(),
        ),
        (
            CLASS.replace("    def", "    @d(1)\n    def"),
            CLASS.replace("    def", "    @d(2)\n    def"),
            changes(definitions="A"),
        ),
        (
            CLASS,
            CLASS.replace("A:", "A(B):") + "\n    def g(self):\n        pass\n",
            changes(definitions="A", used="B"),
        ),
    ],
)
def test_changed_names(old: str, new: str, expected: names.Changes | None) -> None:
    hunks = hunks_between(old, new)
    _, top_lines = selection.find_owners(old, "x.py", hunks)
    assert top_lines is not None
    result = selection.changed_names(
        old,
        new,
        "x.py",
        package="",
        hunks=hunks,
        old_lines=top_lines,
        loaded={"loaded"},
    )
    assert result == expected


@pytest.mark.parametrize(
    ("source", "derives"),
    [
        ("X = Model.Status.choices\n", {"Model", "Status", "choices"}),
        ("X = Model\n", {"Model"}),
        ("X = [views.Go.as_view()]\n", set()),
        ("X = [path('a', views.view)]\n", {"views"}),
        ("X = f(Model.field)\n", {"Model"}),
        ("register(Model.Meta.label)\n", {"Model", "Meta"}),
        (
            (
                "class F(Base):\n    class Meta:\n        model = Product\n"
                "    other = ForeignKey(Other)\n"
            ),
            {"Base", "Product"},
        ),
        ("def f(a=Model.default):\n    pass\n", {"Model"}),
    ],
)
def test_derives(source: str, derives: set[str]) -> None:
    [statement] = names.parse_statements(source)
    assert statement.derives == derives


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from pkg import a\n\nZ = a.X\n", True),
        ("import pkg.a\n\nZ = pkg.a.X\n", True),
        ("from pkg import a as b\n\nclass C:\n    y = b.X\n", True),
        ("from pkg import a\n\n\n@d(a.X)\ndef f(y=a.X):\n    pass\n", True),
        ("from pkg import a\n\nZ = 'a.X'\n", False),
        ("from pkg import a\n\n\ndef f():\n    return a.X\n", False),
        ("from pkg import a\n\nf = lambda: a.X\n", False),
        ("import types\n\nW = types.SimpleNamespace(X=1).X\n", False),
        ("from pkg import b\n\nZ = b.X\n", False),
        ("from pkg.a import X\n", True),
        ("from .a import X as Y\n", True),
        ("from pkg.a import Y\n", False),
        ("Z = X\n", False),
        ("def (\n", True),
    ],
)
def test_needs(source: str, *, expected: bool) -> None:
    assert names.needs(source, "pkg", own=False, module="pkg.a", name="X") is expected


@pytest.mark.parametrize(
    ("version", "source", "expected"),
    [
        ((3, 13), "from pkg import a\n\n\ndef f(x: a.X):\n    pass\n", True),
        ((3, 13), "from pkg import a\n\n\ndef f(*x: a.X) -> a.X:\n    pass\n", True),
        ((3, 13), "from pkg import a\n\n\ndef f(x: int) -> a.X:\n    pass\n", True),
        (
            (3, 13),
            (
                "from __future__ import annotations\n\nfrom pkg import a\n\n\n"
                "def f(x: a.X):\n    pass\n"
            ),
            False,
        ),
        ((3, 14), "from pkg import a\n\n\ndef f(x: a.X):\n    pass\n", False),
        ((3, 13), "from pkg import a\n\nf = lambda x: x\n", False),
    ],
)
def test_needs_annotations(
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int, int],
    source: str,
    *,
    expected: bool,
) -> None:
    monkeypatch.setattr(sys, "version_info", version)
    assert names.needs(source, "pkg", own=False, module="pkg.a", name="X") is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Z = X\n", True),
        ("class C:\n    y = [X]\n", True),
        ("def f():\n    return X\n", False),
        ("X = 1\n", False),
        ("Z = 1\n", False),
    ],
)
def test_needs_own(source: str, *, expected: bool) -> None:
    assert names.needs(source, "pkg", own=True, module="pkg.a", name="X") is expected


def test_libraries() -> None:
    source = "from django import template\n\nregister = template.Library()\n"
    assert names.libraries(source) == {"register"}
    assert names.libraries("def (\n") == frozenset()


def test_changed_names_unparseable() -> None:
    assert names.changed_names("X = (\n", "X = 1\n", "", ({1}, {1}), set()) is None


def test_changed_names_not_registering() -> None:
    old = "@d(1)\ndef f():\n    pass\n"
    lines = ({1}, {1})
    changed = names.changed_names(
        old, "@d(2)\ndef f():\n    pass\n", "", lines, set(), registering=False
    )
    assert changed == names.Changes(definitions=frozenset({"f"}), used=frozenset({"d"}))
    added = "@e\n@d(1)\ndef f():\n    pass\n"
    assert names.changed_names(old, added, "", lines, set(), registering=False) is None


def test_statement_references_skip_function_bodies() -> None:
    source = "class A:\n    @d\n    async def f(self, *, a, b=x):\n        y\n"
    [statement] = names.parse_statements(source)
    assert statement.names == {"d", "x"}


def test_relative_import_resolves_against_package() -> None:
    [statement] = names.parse_statements("from ..b import c\n", "pkg.sub")
    assert statement.modules == ("pkg.b", "pkg.b.c")


def test_find_owners_without_lines() -> None:
    owners, top_lines = selection.find_owners(SOURCE, "x.py", None)
    assert ("Model.method", 15) in owners
    assert top_lines


@pytest.mark.parametrize("source", [None, "def (\n"])
def test_find_owners_unparseable(source: str | None) -> None:
    assert selection.find_owners(source, "x.py", [Hunk(1, 1)]) == ({}, None)


DATA: dict[str, Any] = {
    "files": ["pkg/a.py", "tests/test_a.py", "pkg/data.txt", "pkg/b.py"],
    "functions": [[0, "f", 1], [0, "g", 4]],
    "tests": {
        "tests/test_a.py::test_f": [1, [0], [], [[0, 0]]],
        "tests/test_a.py::test_g": [1, [1], [], []],
    },
    "imports": [],
    "session": [[], [2]],
    "edges": {"pkg/a.py": [], "tests/test_a.py": ["pkg/a.py"], "pkg/b.py": []},
    "modules": {"pkg.a": 0, "pkg.b": 3, "tests.test_a": 1},
    "loaded": [],
    "covered": [],
    "references": {},
    "stars": {},
    "missing": {},
    "names": [],
    "classes": [],
    "inherited": [],
    "libraries": {},
    "builtins": [],
    "enums": [],
    "settings": None,
    "line_sets": [[2]],
}
A_SOURCE = (
    "def f():\n    return 1\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n"
)


def select(
    changes: dict[str, list[Hunk] | None],
    new: str | None = None,
    also_run: list[tuple[str, str]] | None = None,
) -> set[str]:
    selector = selection.Selector(DATA, [], also_run or [])
    return selector.select(changes, lambda _: A_SOURCE, lambda _: new)


def test_full_run_globs(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "settings.py" in plugin.full_run_globs()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert "settings.py" not in plugin.full_run_globs()
    assert "uv.lock" not in plugin.full_run_globs()


@pytest.mark.parametrize(
    ("path", "source", "settings"),
    [
        ("pyproject.toml", "[tool.ruff]\nx = 1\n", None),
        ("pyproject.toml", "[tool.pytest]\nx = 1\n", {"x": 1}),
        ("pyproject.toml", "[tool", "[tool"),
        ("setup.cfg", "[flake8]\nx = 1\n", None),
        ("setup.cfg", "[tool:pytest]\nx = 1\n", {"x": "1"}),
        ("tox.ini", "[pytest]\nx = 1\n", {"x": "1"}),
        ("tox.ini", "x", "x"),
        ("pytest.ini", "[pytest]\n", "[pytest]\n"),
        ("pytest.ini", None, None),
    ],
)
def test_pytest_settings(path: str, source: str | None, settings: object) -> None:
    assert selection.pytest_settings(path, source) == settings


def test_select_function_that_never_ran() -> None:
    assert select({"pkg/a.py": [Hunk(10, 1)]}) == set()


def test_select_deleted_urlconf() -> None:
    data = {
        **DATA,
        "functions": [*DATA["functions"], [3, "path:a/", 0]],
        "tests": {**DATA["tests"], "tests/test_a.py::test_url": [1, [2], [], []]},
    }
    selector = selection.Selector(data, [], [])
    selected = selector.select(
        {"pkg/b.py": None}, lambda _: "urlpatterns = []\n", lambda _: None
    )
    assert selected == {"tests/test_a.py::test_url"}


@pytest.mark.parametrize(
    ("old", "new", "qualname", "expected"),
    [
        (
            "def f(a: int) -> int:\n    return a\n",
            "def f[T](a: T) -> T:\n    return a\n",
            "f",
            True,
        ),
        (
            "class C:\n    def m(self, a: int):\n        pass\n",
            "class C:\n    def m(self, a: str):\n        pass\n",
            "C.m",
            True,
        ),
        (
            "def f():\n    def g(a: int):\n        pass\n",
            "def f():\n    def g(a):\n        pass\n",
            "f.<locals>.g",
            True,
        ),
        ("def f(a=1):\n    pass\n", "def f(a=2):\n    pass\n", "f", False),
        ("def f():\n    return 1\n", "def f():\n    return 2\n", "f", False),
        ("def f():\n    pass\n", "def g():\n    pass\n", "f", False),
        ("def f():\n    pass\n", "def (\n", "f", False),
    ],
)
def test_annotations_only(old: str, new: str, qualname: str, *, expected: bool) -> None:
    assert selection.annotations_only(old, new, qualname) is expected


TEMPLATE = (
    "{% load i18n %}\n"
    "<p>{{ a }}</p>\n"
    "{% if x %}\n"
    "  <b>{{ b }}</b>\n"
    "{% else %}\n"
    "  c\n"
    "{% endif %}\n"
    "{% url 'home' %}\n"
    "end\n"
)


@pytest.mark.parametrize(
    ("source", "spans", "expected"),
    [
        (TEMPLATE, [(2, 1)], {2}),
        (TEMPLATE, [(4, 1)], {4}),
        (TEMPLATE, [(5, 1)], {3, 4, 5, 6}),
        (TEMPLATE, [(7, 1)], {3, 4, 5, 6, 7}),
        (TEMPLATE, [(3, 0)], {3, 4, 5, 6}),
        ("{% verbatim %}\na\n{% endverbatim %}\n", [(2, 1)], {1}),
        (TEMPLATE, [(1, 1)], None),
        (TEMPLATE, [(8, 0)], None),
        ("{% extends 'base.html' %}\n", [(1, 1)], None),
        ("a\n{% else %}\n", [(2, 1)], None),
        ("a\n{% endif %}\n", [(2, 1)], {1, 2}),
    ],
)
def test_template_heads(
    source: str, spans: list[tuple[int, int]], expected: set[int] | None
) -> None:
    assert templates.heads(source, spans) == expected


def test_template_heads_without_django(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "django.template.base", None)
    assert templates.heads(TEMPLATE, [(2, 1)]) is None


def test_render_without_origin() -> None:
    django_hooks = importlib.import_module("pytest_blast_radius.django_hooks")
    rendered: list[tuple[str, int]] = []
    wrapper = django_hooks._render_wrap(
        lambda node, context: "text",
        lambda name, line: rendered.append((name, line)),
    )
    assert wrapper(types.SimpleNamespace(), None) == "text"
    assert rendered == []


def test_django_without_settings() -> None:
    django_hooks = importlib.import_module("pytest_blast_radius.django_hooks")
    assert django_hooks.template_libraries() == ({}, [])
    assert django_hooks._root_urlconf_file() is None


def test_django_without_factory_boy(monkeypatch: pytest.MonkeyPatch) -> None:
    django_hooks = importlib.import_module("pytest_blast_radius.django_hooks")
    monkeypatch.setitem(sys.modules, "factory.builder", None)
    assert django_hooks._factory_classes() == (None, None)
    assert "build" not in {name for _, name, _ in django_hooks.patches()}
    assert "BaseFactory" not in {base.__name__ for base in django_hooks.covered()}


def test_local_memory_cache_reads() -> None:
    django_hooks = importlib.import_module("pytest_blast_radius.django_hooks")
    locmem = importlib.import_module("django.core.cache.backends.locmem")
    events: list[str] = []
    patches = django_hooks._store_patches(lambda event, _: events.append(event))
    wrappers: dict[str, Any] = {name: wrapper for _, name, wrapper in patches}
    cache = locmem.LocMemCache("test-reads", {})
    assert not wrappers["has_key"](cache, "k")
    wrappers["_set"](cache, cache.make_and_validate_key("k"), b"\x80\x04K\x01.")
    assert wrappers["has_key"](cache, "k")
    assert events == ["miss", "set", "hit"]


def test_stored_values(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    recorder.start_test("a", "t.py")
    # Stored without a read missing it: what the test ran, which a read in the
    # same test already has.
    recorder.stored("warm")
    recorder.stored_hit("warm")
    recorder.stored_miss("k")
    recorder.cache_stack.insert(0, (None, set()))
    for sink in recorder.sinks:
        sink.add(("m.py", "compute", 1))
    recorder.stored("k")
    # Storing it again keeps what computed it, not all the test ran.
    computed = recorder.stored_values["k"]
    recorder.stored("k")
    assert recorder.stored_values["k"] is computed
    recorder.finish_test([])
    recorder.start_test("b", "t.py")
    recorder.stored_hit("k")
    recorder.stored_hit("unknown")
    assert ("m.py", "compute", 1) in recorder.base


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            (
                "class A(models.TextChoices):\n    X = 1\n\n\nclass B:\n    pass\n\n\n"
                "class C(enum.IntFlag):\n    Y = 1\n"
            ),
            {"A", "C"},
        ),
        ("def (", set()),
    ],
)
def test_enum_classes(source: str, expected: set[str]) -> None:
    assert selection.enum_classes(source) == expected


def test_settings_without_module() -> None:
    django_hooks = importlib.import_module("pytest_blast_radius.django_hooks")
    assert django_hooks._settings_patches(lambda name, detail: None) == []


def test_render_line_outside_tests(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    recorder.render_line(str(tmp_path / "page.html"), 1)
    recorder.render_line("/elsewhere/page.html", 1)
    assert recorder.session == {("page.html", templates.TEMPLATE, 1)}


def test_select_settings() -> None:
    settings = "pkg/settings.py"
    data = {
        **DATA,
        "files": [*DATA["files"], settings],
        "functions": [*DATA["functions"], [4, "setting:A", 0], [4, "setting:B", 0]],
        "tests": {**DATA["tests"], "tests/test_a.py::test_a": [1, [2], [], []]},
        "session": [[3], []],
        "settings": settings,
    }
    old = "A = 1\nB = 2\nC = B\n"

    def select(new: str) -> set[str]:
        selector = selection.Selector(data, ["settings.py"], [])
        changes = {settings: hunks_between(old, new)}
        return selector.select(changes, lambda _: old, lambda _: new)

    assert select("A = 9\nB = 2\nC = B\n") == {"tests/test_a.py::test_a"}
    with pytest.raises(selection.RunAllError, match="B, which is read outside"):
        select("A = 1\nB = 3\nC = B\n")
    with pytest.raises(selection.RunAllError, match=r"pkg/settings\.py changed"):
        select("A = 1\nB = 2\nC = B\nif A:\n    C = 3\n")


def test_select_migration() -> None:
    migration = "pkg/migrations/0002_more.py"
    data = {
        **DATA,
        "files": [*DATA["files"], "pkg/migrations/0001_initial.py"],
        "functions": [*DATA["functions"], [4, "forwards", 3]],
        "tests": {
            **DATA["tests"],
            "tests/test_a.py::test_file": [1, [], [4], []],
            "tests/test_a.py::test_function": [1, [2], [], []],
        },
    }
    selector = selection.Selector(data, ["*/migrations/*"], [])
    assert selector.select({migration: None}, lambda _: None, lambda _: "") == {
        "tests/test_a.py::test_file",
        "tests/test_a.py::test_function",
    }
    data = {
        **data,
        "functions": [*data["functions"], [0, "Item", 0]],
        "tests": {**data["tests"], "tests/test_a.py::test_item": [1, [3], [], []]},
        "classes": [[0, "Item", 0, [], False, 0]],
        "inherited": [[]],
    }
    selector = selection.Selector(data, ["*/migrations/*"], [])

    def select(operation: str) -> set[str]:
        source = f"class Migration:\n    operations = [{operation}]\n"
        return selector.select({migration: None}, lambda _: None, lambda _: source)

    assert "tests/test_a.py::test_item" in select(
        "migrations.AddField(model_name='item', name='x', field=None)"
    )
    assert "tests/test_a.py::test_item" not in select(
        "migrations.AlterModelOptions(name='item', options={})"
    )
    with pytest.raises(selection.RunAllError, match="runs code"):
        select("migrations.RunPython(forwards)")
    also = selection.Selector(data, [], [("*/migrations/*", "tests/test_a.py")])
    assert "tests/test_a.py::test_g" in also.select(
        {migration: None}, lambda _: None, lambda _: ""
    )
    selector = selection.Selector(DATA, ["*/migrations/*"], [])
    with pytest.raises(selection.RunAllError, match=r"0002_more\.py changed"):
        selector.select({migration: None}, lambda _: None, lambda _: "")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (None, set()),
        ("", set()),
        ("operations = [AddField('Item', 'x', None)]", {"item"}),
        ("operations = (CreateModel(name='Item', fields=[]),)", {"item"}),
        ("operations = [RenameModel('Old', new_name='New')]", {"old", "new"}),
        ("operations = [AlterModelOptions(name='item', options={})]", set()),
        ("operations = [AddTrigger(model_name='item', trigger=None)]", {"item"}),
        ("operations = [CreateExtension('hstore')]", None),
        ("operations = [RunPython(forwards)]", None),
        ("operations = [AddField(model_name=NAME)]", None),
        ("operations = [factory()()]", None),
        ("operations = BASE + []", None),
        ("operations = [OPERATION]", None),
        ("operations: list = []", None),
        ("operations = []\noperations += [RunPython(f)]", None),
        ("operations = []\noperations.append(RunPython(f))", None),
        ("operations = [\n", None),
    ],
)
def test_migration_models(source: str | None, expected: set[str] | None) -> None:
    assert migrations.models(source) == expected


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("@fixture\ndef a():\n    pass\n", "@fixture\ndef a():\n    return 1\n", set()),
        ("", "@pytest.fixture(name='b')\ndef a():\n    pass\n", {"b"}),
        ("@fixture\ndef a():\n    pass\n", "@fixture\ndef a(b):\n    pass\n", {"a"}),
        ("def a():\n    pass\n", "@other\ndef a():\n    pass\n", set()),
        ("def (", "", None),
        ("", "def (", None),
    ],
)
def test_changed_fixtures(old: str, new: str, expected: set[str] | None) -> None:
    assert selection.changed_fixtures(old, new) == expected


def test_select_changed_fixture() -> None:
    data = {
        **DATA,
        "files": [*DATA["files"], "a/test_a.py", "b/test_b.py", "a/conftest.py"],
        "functions": [
            *DATA["functions"],
            [4, "fixture:value", 0],
            [5, "fixture:value", 0],
        ],
        "tests": {
            **DATA["tests"],
            "a/test_a.py::test_a": [4, [2], [], []],
            "b/test_b.py::test_b": [5, [3], [], []],
        },
    }
    selector = selection.Selector(data, [], [])

    def select(path: str) -> set[str]:
        old = "@pytest.fixture\ndef value():\n    return 1\n"
        new = old.replace("()", "(other)")
        return selector.select({path: None}, lambda _: old, lambda _: new)

    assert select("a/conftest.py") == {"a/test_a.py::test_a"}
    assert select("a/test_a.py") >= {"a/test_a.py::test_a"}
    assert "b/test_b.py::test_b" not in select("a/test_a.py")
    assert select("plugin.py") >= {"a/test_a.py::test_a", "b/test_b.py::test_b"}
    assert select("conftest.py") >= {"a/test_a.py::test_a", "b/test_b.py::test_b"}
    # A file that doesn't parse changes as a whole module.
    assert "b/test_b.py::test_b" not in selector.select(
        {"a/conftest.py": None}, lambda _: "", lambda _: "def ("
    )


def test_touch_directory_and_definition(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    recorder.start_test("test_a", "test_a.py")
    recorder.touch(str(tmp_path / "cases"), directory=True)
    recorder.credit_definition(compile("pass", "/elsewhere/x.py", "exec"))
    recorder.finish_test([])
    assert recorder.tests["test_a"][1] == {"cases/"}


@pytest.mark.parametrize(
    "reader",
    ["<frozen importlib._bootstrap_external>", f"{os.sep}lib{os.sep}linecache.py"],
)
def test_touch_for_code(tmp_path: Path, reader: str) -> None:
    recorder = recording.Recorder(tmp_path)
    recorder.start_test("test_a", "test_a.py")
    namespace: dict[str, Any] = {}
    source = "def read(recorder, path):\n    recorder.touch(path)\n"
    exec(compile(source, reader, "exec"), namespace)  # noqa: S102
    namespace["read"](recorder, str(tmp_path / "a.py"))
    recorder.finish_test([])
    assert recorder.tests["test_a"][1] == set()


def test_select_source_read_outside_tests() -> None:
    data = {
        **DATA,
        "functions": [*DATA["functions"], [0, recording.SOURCE, 0]],
        "session": [[2], []],
    }
    selector = selection.Selector(data, [], [])
    with pytest.raises(selection.RunAllError, match="read outside tests"):
        selector.select({"pkg/a.py": None}, lambda _: "", lambda _: "X = 1\n")


def test_realpath(tmp_path: Path) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "relative").symlink_to("real/sub")
    (tmp_path / "absolute").symlink_to(tmp_path / "real")
    (tmp_path / "loop").symlink_to("loop")
    for path in [
        "relative/../x.py",
        "absolute/./sub/x.py",
        "loop/x.py",
        "missing/../real",
    ]:
        full = f"{tmp_path}/{path}"
        assert recording._realpath(full) == os.path.realpath(full)


def test_touch_root_through_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    recorder = recording.Recorder(tmp_path / "link")
    recorder.start_test("test_a", "test_a.py")
    recorder.touch(str(tmp_path / "link"), directory=True)
    recorder.touch(str(tmp_path / "real" / "a.txt"))
    recorder.finish_test([])
    assert recorder.tests["test_a"][1] == {"/", "a.txt"}


def test_incidental_with_only_stdlib_callers(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    namespace: dict[str, Any] = {}
    source = (
        "def check(recorder, results):\n"
        "    results.append(recorder.incidental(libraries=True))\n"
    )
    stdlib = sysconfig.get_paths()["stdlib"]
    exec(compile(source, f"{stdlib}/blast_radius_check.py", "exec"), namespace)  # noqa: S102
    results: list[bool] = []
    thread = threading.Thread(target=namespace["check"], args=(recorder, results))
    thread.start()
    thread.join()
    assert results == [True]


def test_select_listed_directory() -> None:
    data = {
        **DATA,
        "files": [*DATA["files"], "cases/"],
        "tests": {**DATA["tests"], "tests/test_a.py::test_cases": [1, [], [4], []]},
        "session": [[], []],
    }
    changes: dict[str, list[selection.Hunk] | None] = {"cases/new.txt": None}
    selector = selection.Selector(data, [], [])
    assert selector.select(changes, lambda _: None, lambda _: "x") == {
        "tests/test_a.py::test_cases"
    }
    selector = selection.Selector({**data, "session": [[], [4]]}, [], [])
    with pytest.raises(selection.RunAllError, match="listed outside tests"):
        selector.select(changes, lambda _: None, lambda _: "x")


def test_select_changed_all_without_star_importer() -> None:
    selector = selection.Selector({**DATA, "stars": {"pkg/b.py": ["other"]}}, [], [])
    old, new = "__all__ = ['x']\n", "__all__ = []\n"
    changes: dict[str, list[selection.Hunk] | None] = {
        "pkg/a.py": hunks_between(old, new)
    }
    assert selector.select(changes, lambda _: old, lambda _: new) == set()


def test_select_value_read_by_class_body() -> None:
    data = {
        **DATA,
        "functions": [*DATA["functions"], [0, "C", 5], [0, "C", 0]],
        "tests": {**DATA["tests"], "tests/test_a.py::test_c": [1, [2, 3], [], []]},
        "imports": [[3, [2, 3], []]],
        "names": [[2, ["X"], [], []], [3, [], ["X"], []]],
        # The class body ran outside tests, and no test credited the class.
        "session": [[2], []],
    }
    old = "X = 1\n\n\nclass C:\n    y = X\n"
    new = old.replace("X = 1", "X = 2")
    selector = selection.Selector(data, [], [])
    changes: dict[str, list[selection.Hunk] | None] = {
        "pkg/a.py": hunks_between(old, new)
    }
    assert "tests/test_a.py::test_c" in selector.select(
        changes, lambda _: old, lambda _: new
    )


def test_select_compiled_template() -> None:
    data = {
        **DATA,
        "functions": [*DATA["functions"], [2, "root", 1]],
        "tests": {**DATA["tests"], "tests/test_a.py::test_page": [1, [2], [], []]},
        "session": [[], []],
    }
    selector = selection.Selector(data, [], [])
    assert selector.select({"pkg/data.txt": None}, lambda _: "", lambda _: "") == {
        "tests/test_a.py::test_page"
    }


def test_select_unrecorded_file() -> None:
    assert select({"pkg/other.txt": None}) == set()


def test_select_file_read_outside_tests() -> None:
    with pytest.raises(
        selection.RunAllError, match=r"pkg/data.txt is read outside tests"
    ):
        select({"pkg/data.txt": None})


def test_select_name_registered_elsewhere() -> None:
    references = {"X": [["pkg/b.py", "other", [], "attribute", [], False]]}
    edges = {**DATA["edges"], "pkg/b.py": ["pkg/a.py"]}
    selector = selection.Selector(
        {**DATA, "references": references, "edges": edges}, [], []
    )
    with pytest.raises(
        selection.RunAllError,
        match=r"^pkg/b.py uses X from pkg/a.py at import time and nothing imports it$",
    ):
        selector.select(
            {"pkg/a.py": [Hunk(1, 1, ["X = 2"], new_start=1, new_count=1)]},
            lambda _: "X = 1\n",
            # b.py registers something as it loads.
            lambda path: "X = 2\n" if path == "pkg/a.py" else "register(X)\n",
        )
    covered = selection.Selector(
        {**DATA, "references": references, "edges": edges, "covered": ["X"]}, [], []
    )
    assert (
        covered.select(
            {"pkg/a.py": [Hunk(1, 1, ["X = 2"], new_start=1, new_count=1)]},
            lambda _: "X = 1\n",
            lambda _: "X = 2\n",
        )
        == set()
    )


def test_select_top_level_change_imported_by_unimported_module() -> None:
    data = {**DATA, "edges": {**DATA["edges"], "pkg/c.py": ["pkg/b.py"]}}
    selector = selection.Selector(data, [], [])
    with pytest.raises(
        selection.RunAllError,
        match=r"^top-level code in pkg/b.py changed and pkg/c.py imports it$",
    ):
        selector.select(
            {"pkg/b.py": None},
            lambda _: "X = 1\n",
            lambda path: "X = 2\n" if path == "pkg/b.py" else "register()\n",
        )


def test_select_also_run() -> None:
    also_run = [
        ("*.txt", "tests/test_a.py"),
        ("*.md", "tests/test_b.py"),
        ("*.md", "tests/test_a.py"),
    ]
    assert select({"README.md": None}, also_run=also_run) == set(DATA["tests"])


def test_snapshot_and_changes(pytester: pytest.Pytester) -> None:
    root = pytester.path
    storage.run_git(root, "init", "-q")
    files = {"a.py": "x = 1\n", "b.bin": "1", ".gitignore": "ignored.txt\n"}
    for name, content in files.items():
        (root / name).write_text(content)
    storage.run_git(root, "add", "-A")
    storage.run_git(
        root, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", "A"
    )
    repository = storage.find_repository(root)
    base = repository.snapshot()
    assert base == repository.git("rev-parse", "HEAD^{tree}").strip()
    (root / "a.py").write_text("x = 2\n")
    (root / "b.bin").chmod(0o755)
    (root / "c.py").write_text("")
    (root / "ignored.txt").write_text("")
    tree = repository.snapshot()
    assert selection.find_changes(repository, base, tree) == {
        "a.py": [Hunk(1, 1, ["x = 2"], new_start=1, new_count=1)],
        "b.bin": None,
        "c.py": None,
    }
    assert repository.git("diff", "--cached", "--name-only") == ""
    assert repository.source(base, "missing.py") is None


ENVIRONMENT = {"python": "3", "format": storage.FORMAT_VERSION, "used": "1"}
INSTALLED = {**ENVIRONMENT, "unused": "1"}


def test_load_closest_recording(pytester: pytest.Pytester) -> None:
    storage.run_git(pytester.path, "init", "-q")
    repository = storage.find_repository(pytester.path)
    assert repository.load("0" * 40, INSTALLED) is None
    trees = []
    for content in ("1", "2", "3"):
        (pytester.path / "a.txt").write_text(content)
        (pytester.path / f"{content}.txt").write_text("")
        trees.append(repository.snapshot())
    for tree in [*trees[:2], "0" * 40]:
        repository.save({"tree": tree, "environment": ENVIRONMENT})
    closest = repository.load(trees[2], {**INSTALLED, "unused": "2"})
    assert closest is not None
    assert closest["tree"] == trees[1]
    assert repository.load(trees[2], {**INSTALLED, "used": "2"}) is None
    next(repository.directory.glob(f"*-{'0' * 40}.environment.json")).write_text("{")
    repository.save({"tree": trees[1], "environment": {"python": "3"}})
    closest = repository.load(trees[2], INSTALLED)
    assert closest is not None
    assert closest["tree"] == trees[0]


def test_parsed_modules(pytester: pytest.Pytester) -> None:
    storage.run_git(pytester.path, "init", "-q")
    repository = storage.find_repository(pytester.path)
    assert repository.load_parsed() == {}
    repository.directory.mkdir()
    old = repository.directory / "parsed-0-3.12.gz"
    old.write_bytes(b"")
    repository.save_parsed({"key": [[], []]})
    assert repository.load_parsed() == {"key": [[], []]}
    assert not old.exists()
    [path] = repository.directory.glob("parsed-*.gz")
    path.write_bytes(b"not gzip")
    assert repository.load_parsed() == {}


def test_storage_keeps_recently_used_maps(pytester: pytest.Pytester) -> None:
    storage.run_git(pytester.path, "init", "-q")
    repository = storage.find_repository(pytester.path)
    for index in range(storage.KEEP + 2):
        repository.save({"tree": f"{index:040}", "environment": ENVIRONMENT})
    assert len(list(repository.directory.glob("*.json.gz"))) == storage.KEEP
    assert len(list(repository.directory.glob("*.environment.json"))) == storage.KEEP


def test_recording_removed_while_listing(tmp_path: Path) -> None:
    assert storage._mtime(tmp_path / "gone.json.gz") == 0


def test_environment_keeps_used_distributions() -> None:
    packages = {**storage.installed(), "not-imported": "1"}
    environment = storage.environment(["pytest", "_pytest.main"], packages)
    assert environment["python"] == sys.version
    assert "pytest" in environment
    assert "pytest-xdist" in environment
    assert "not-imported" not in environment


def test_environment_notes_absent_modules() -> None:
    environment = storage.environment(
        [], {}, ["json", "blast_radius_absent", "", ".bad"]
    )
    assert environment == {
        "absent:blast_radius_absent": "",
        "absent:": "",
        "absent:.bad": "",
    }


def test_git_missing(tmp_path: Path) -> None:
    with pytest.raises(storage.GitError):
        storage.run_git(tmp_path / "missing", "status")


def test_no_free_tool_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(recording, "TOOL_IDS", ())
    with pytest.raises(RuntimeError, match=r"no free sys\.monitoring tool id"):
        recording.Recorder(tmp_path).start()


def test_relative(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    assert recorder.relative(str(tmp_path / "a" / "b.py")) == "a/b.py"
    assert recorder.relative(str(tmp_path / ".git" / "HEAD")) is None
    assert recorder.relative("/elsewhere/c.py") is None
    assert recorder.relative(f"{tmp_path}/lib/site-packages/d.py") is None


def test_touch(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    recorder.start_test("test_a", "test_a.py")
    # Only project code reads a Python file as data.
    namespace: dict[str, Any] = {}
    source = "def read(recorder, path):\n    recorder.touch(path)\n"
    exec(compile(source, str(tmp_path / "reader.py"), "exec"), namespace)  # noqa: S102
    namespace["read"](recorder, str(tmp_path / "a.py"))
    recorder.touch(str(tmp_path / "b.py"))
    recorder.touch(str(tmp_path / "a.txt"))
    recorder.touch(str(tmp_path / "__pycache__/a.pyc"))
    recorder.touch("/elsewhere/b.txt")
    recorder.credit_class(None)
    recorder.credit_class(Fixture)
    recorder.credit_class(Fixture)
    # A Python file read as data has a key of its own.
    assert recorder.tests["test_a"] == (
        "test_a.py",
        {"a.txt", ("a.py", recording.SOURCE, 0)},
        {},
    )


def test_touch_outside_tests_counts_project_reads_only(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    recorder.touch(str(tmp_path / "tool.toml"))
    (tmp_path / "reader.py").write_text(
        "def read(recorder, path):\n    recorder.touch(path)\n"
    )
    spec = importlib.util.spec_from_file_location("reader", tmp_path / "reader.py")
    assert spec is not None
    assert spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    reader.read(recorder, str(tmp_path / "data.json"))
    assert recorder.session == {"data.json"}


class Fixture:
    def __init__(self, argname: str) -> None:
        self.argname = argname


def test_fixture_credit(tmp_path: Path) -> None:
    recorder = recording.Recorder(tmp_path)
    used = Fixture("used")
    unused = Fixture("unused")
    recorder.start_test("test_a", "test_a.py")
    for fixturedef in (used, unused):
        keys = recorder.start_fixture()
        keys.add(fixturedef.argname)
        recorder.finish_fixture(fixturedef, keys)
    recorder.finish_fixture(used, {"twice"})
    recorder.finish_test([])
    recorder.start_test("test_b", "test_b.py")
    recorder.finish_test(["used"])
    recorder.drop_fixture(used)
    recorder.drop_fixture(used)
    assert recorder.tests["test_b"] == (
        "test_b.py",
        {"twice", ("test_b.py", "fixture:used", 0)},
        {},
    )


def test_caches_and_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("blast_radius_cached")
    module.__file__ = str(tmp_path / "blast_radius_cached.py")
    calls = []

    @functools.cache
    def counted() -> int:
        calls.append(1)
        return 1

    class Holder:
        static = staticmethod(counted)
        plain = staticmethod(len)
        builtin = staticmethod(functools.cache(len))

    Holder.__module__ = module.__name__
    # A class referring to itself, as through Meta.model, is visited once.
    Holder.itself = Holder  # ty: ignore[unresolved-attribute]
    module.Holder = Holder  # ty: ignore[unresolved-attribute]
    native = types.ModuleType("blast_radius_native")
    native.__file__ = str(tmp_path / "native.so")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, native.__name__, native)
    monkeypatch.setitem(sys.modules, "blast_radius_no_dict", 1)
    recorder = recording.Recorder(tmp_path)
    recorder.start_test("test_a", "test_a.py")
    counted()
    recorder.finish_test([])
    # What the miss ran, which monitoring would have collected.
    recorder.cached[0][1].add("computed")
    recorder.start_test("test_b", "test_b.py")
    counted()
    recorder.finish_test([])
    # Caches stay, so tests end as they would without recording.
    assert len(calls) == 1
    assert recorder.tests["test_a"][1] == set()
    assert recorder.tests["test_b"][1] == {"computed"}
    assert list(recorder.export()["modules"]) == [module.__name__]


def test_caches_found_after_use(tmp_path: Path) -> None:
    @functools.cache
    def used() -> int:
        return 1

    @functools.cache
    def loaded() -> int:
        return 1

    class Looped:
        pass

    looped = Looped()
    looped.__wrapped__ = looped  # ty: ignore[unresolved-attribute]
    used()
    used()
    recorder = recording.Recorder(tmp_path)
    recorder.find_caches([{"used": used}], {"filled"})
    # What computed its values, and, as it was hit, the session.
    assert recorder.cached[0][1] == {"filled"}
    assert "filled" in recorder.session
    code = compile("", str(tmp_path / "m.py"), "exec")
    frames = {}
    source = (
        "import sys\n\n\ndef here(inner=None):\n    return (inner or sys._getframe)()\n"
    )
    for name in ("loader", "m", "user", "<frozen importlib._bootstrap>"):
        namespace: dict[str, Any] = {}
        path = name if name.startswith("<") else str(tmp_path / f"{name}.py")
        exec(compile(source, path, "exec"), namespace)  # noqa: S102
        frames[name] = namespace["here"]
    # The loader calls the import system, whose frame comes first.
    importing = frames["loader"](frames["<frozen importlib._bootstrap>"])
    recorder.module_loaded(code, {"__name__": "m", "loaded": loaded}, importing)
    recorder.module_loaded(code, {"__name__": "m"}, frames["m"]())
    elsewhere = compile("", "/elsewhere/m.py", "exec")
    recorder.module_loaded(
        elsewhere, {"__name__": "m", "other": functools.cache(len)}, None
    )
    assert len(recorder.cached) == 2  # noqa: PLR2004
    # Loading outside a test may affect any test.
    assert recorder.loaders == {}
    # The function that loaded a module during a test, but not an import statement.
    recorder.start_test("t", "t.py")
    other = compile("", str(tmp_path / "n.py"), "exec")
    recorder.module_loaded(other, {"__name__": "n"}, importing)
    here = frames["loader"]
    statement = types.FunctionType(
        here.__code__.replace(co_name="<module>"), here.__globals__, None, (None,)
    )
    recorder.module_loaded(code, {"__name__": "m"}, statement())
    assert recorder.loaders == {"n.py": {"loader.py"}}
    # A module whose top level loads one through a project function imports it.
    user = frames["user"]
    top = types.FunctionType(
        user.__code__.replace(co_name="<module>"), user.__globals__, None, (None,)
    )
    load = functools.partial(here, frames["<frozen importlib._bootstrap>"])
    recorder.module_loaded(other, {"__name__": "n"}, top(load))
    assert recorder.loads == {"user.py": {"n.py"}}
    assert recording._cached_code(functools.partial(loaded.__wrapped__)) is not None
    assert recording._cached_code(looped) is None


def test_analyse_modules(tmp_path: Path) -> None:
    files = {
        "pkg/__init__.py": "from . import a\n",
        "pkg/a.py": (
            "from .sub import b\n\nX = b.Y\n\n\ndef f():\n    from os import path\n"
        ),
        "pkg/sub/__init__.py": "from .b import *\n",
        "pkg/sub/b.py": "from .. import a\nfrom ..sub import b\nimport os\n\nY = 1\n",
        "broken.py": "def (\n",
    }
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    modules = {
        "pkg": "pkg/__init__.py",
        "pkg.a": "pkg/a.py",
        "pkg.sub": "pkg/sub/__init__.py",
        "pkg.sub.b": "pkg/sub/b.py",
        "broken": "broken.py",
        "missing": "missing.py",
    }
    analysis = recording.analyse_modules(tmp_path, modules, {})
    assert analysis == recording.analyse_modules(tmp_path, modules, analysis.parsed)
    assert analysis.edges == {
        "pkg/__init__.py": ["pkg/a.py"],
        "pkg/a.py": ["pkg/__init__.py", "pkg/sub/__init__.py", "pkg/sub/b.py"],
        "pkg/sub/__init__.py": ["pkg/__init__.py", "pkg/sub/b.py"],
        "pkg/sub/b.py": ["pkg/__init__.py", "pkg/a.py", "pkg/sub/__init__.py"],
    }
    assert analysis.defined == {"a", "b", "os", "X", "Y", "f"}
    assert analysis.references["Y"] == [
        ["pkg/a.py", "assign", ["X"], "attribute", [], True]
    ]
    assert "path" not in analysis.references
    assert analysis.stars == {"pkg/sub/__init__.py": ["pkg.sub.b", "pkg.sub.b.*"]}
    # Module objects imports bind, but not the modules names come from.
    assert analysis.holds == {
        "pkg/__init__.py": ["pkg/a.py"],
        "pkg/a.py": ["pkg/sub/b.py"],
        "pkg/sub/b.py": ["pkg/a.py"],
    }


@pytest.mark.parametrize(
    ("route", "regex", "endpoint", "matches", "misses"),
    [
        ("items/", False, False, ["items/", "items/3"], ["myitems/", "x/items/"]),
        ("items/", False, True, ["items/"], ["items/3"]),
        ("<int:pk>/edit/", False, True, ["12/edit/"], ["b/edit/"]),
        ("<pk>/", False, True, ["b/"], ["b/c/"]),
        ("<custom:pk>/", False, True, ["b/c/"], []),
        (r"^items/(?P<pk>\d+)/$", True, True, ["items/3/"], ["items/x/"]),
        (r"items$", True, True, ["myitems"], ["items/"]),
    ],
)
def test_route_pattern(
    route: str, *, regex: bool, endpoint: bool, matches: list[str], misses: list[str]
) -> None:
    pattern = urlconfs.route_pattern(route, regex=regex, endpoint=endpoint)
    assert all(pattern.search(path) for path in matches)
    assert not any(pattern.search(path) for path in misses)


URLS = """\
from django.urls import include, path, re_path

from shop import views

urlpatterns = [
    path("a/", views.a, name="a"),
    path("b/", views.B.as_view(), name="b"),
]
"""


@pytest.mark.parametrize(
    ("new", "routes", "names", "includes"),
    [
        (URLS.replace('"a/"', '"aa/"'), ["a/", "aa/"], {"a"}, False),
        (URLS.replace('name="b"', 'name="c"'), ["b/"], {"b", "c"}, False),
        (
            URLS.replace("]\n", '    path("c/", include("shop.c")),\n]\n'),
            ["c/x/"],
            set(),
            True,
        ),
        (
            URLS.replace("]\n", '    path("d/", views.d, None, "d"),\n]\n'),
            ["d/"],
            {"d"},
            False,
        ),
        (
            URLS + 'urlpatterns += [re_path(r"^e/$", views.e)]\n',
            ["e/"],
            set(),
            False,
        ),
        (
            URLS + 'urlpatterns = urlpatterns + [path("f/", admin.site.urls)]\n',
            ["f/x/"],
            set(),
            True,
        ),
        (
            URLS.replace('    path("a/", views.a, name="a"),\n', "").replace(
                "]\n", '    path("a/", views.a, name="a"),\n]\n'
            ),
            ["a/", "b/"],
            {"a", "b"},
            False,
        ),
    ],
)
def test_url_changes(
    new: str, routes: list[str], names: set[str], *, includes: bool
) -> None:
    change = urlconfs.changes(URLS, new)
    assert change is not None
    assert all(any(p.search(route) for p in change.paths) for route in routes)
    assert change.names == names
    assert change.includes is includes


@pytest.mark.parametrize(
    "new",
    [
        URLS.replace('"a/"', "ROUTE"),
        URLS.replace('name="a"', "name=NAME"),
        URLS.replace("]\n", "    *extra(),\n]\n"),
        URLS + "urlpatterns = build()\n",
        URLS + "urlpatterns += extra_patterns\n",
        URLS + "def view(request):\n    pass\n",
        "urlpatterns = [\n",
    ],
)
def test_url_changes_doing_more(new: str) -> None:
    assert urlconfs.changes(URLS, new) is None


MODULES = {
    "shop": "",
    "shop.views": "def a(request):\n    pass\n\n\nclass B:\n    pass\n",
    "shop.c": "urlpatterns = []\n",
    "shop.starred": "from shop.views import *\n",
    "shop.broken": "def (\n",
}
LOCAL = URLS + "\n\ndef local(request):\n    pass\n"


def added(entry: str) -> str:
    return URLS.replace("]\n", f"    {entry},\n]\n")


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (URLS, added('path("c/", views.a, name="c")'), False),
        (URLS, added('path("d/", views.missing)'), True),
        (URLS, added('path("e/", include("shop.c"))'), False),
        (URLS, added('path("e/", include("shop.nothing"))'), True),
        (URLS, added('path("f/", include("django.contrib.auth.urls"))'), False),
        (URLS, added('path("g/", undefined)'), True),
        (URLS, added('path("h/", str)'), False),
        (URLS, URLS.replace("import views", "import other"), True),
        (URLS, "from shop.starred import *\n" + URLS, False),
        (URLS, "from shop.broken import x\n" + URLS, True),
        (URLS, "import shop.views\n" + URLS, False),
        (URLS, "import shop.views as shop_views\n" + URLS, False),
        (LOCAL, LOCAL + 'urlpatterns += [path("l/", local)]\n', False),
    ],
)
def test_unresolved(old: str, new: str, *, expected: bool) -> None:
    change = urlconfs.changes(old, new)
    assert change is not None
    assert urlconfs.unresolved(new, "shop", change, MODULES.get) is expected


def test_patch_report(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    sources = {
        "pkg/m.py": (
            "def f(a):\n    if a:\n        return 1\n    return 2\n\n\n"
            "def g():\n    return 3\n"
        ),
        "pkg/unrun.py": "X = 1\n",
        "other.py": "Y = 1\n",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source)
    cov = coverage.Coverage(data_file=None, branch=True, source=["pkg"])
    # Importing pkg.m and calling f(1) ran these.
    arcs = [(-1, 1), (1, 7), (7, -1), (-1, 2), (2, 3), (3, -1)]
    cov.get_data().add_arcs({str(tmp_path / "pkg/m.py"): arcs})
    changes: dict[str, list[Hunk] | None] = {
        "pkg/m.py": [Hunk(2, 3, new_start=2, new_count=3), line(8)],
        "pkg/unrun.py": [line(1)],
        "other.py": [line(1)],
        "gone.py": [line(1)],
        "notes.txt": [line(1)],
        "binary.py": None,
    }
    root = tmp_path
    assert patch.report(cov, root, changes) == [
        "blast-radius: tests cover 1 of 5 changed lines; not:",
        "- pkg/m.py: 4, 8, partly 2",
        "- pkg/unrun.py: 1",
    ]
    assert patch.report(cov, root, {"pkg/m.py": [line(3)]}) == [
        "blast-radius: tests cover all 1 changed lines"
    ]
    assert patch.report(cov, root, {"pkg/m.py": [line(5)]}) == []
    cov.get_data().close(force=True)


def line(number: int) -> Hunk:
    return Hunk(number, 1, new_start=number, new_count=1)


def test_requested(tmp_path: Path) -> None:
    config: Any = types.SimpleNamespace(
        args=["/elsewhere/test_a.py", "../tests/test_b.py::test_b"],
        invocation_params=types.SimpleNamespace(dir=tmp_path / "root" / "docs"),
        rootpath=tmp_path / "root",
    )
    assert not plugin._requested(config, "test_a.py::test_a")
    assert plugin._requested(config, "tests/test_b.py::test_b[1]")
