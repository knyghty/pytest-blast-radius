"""Recording leaves what tests see as it was."""

from tests.conftest import Project
from tests.test_misses import DJANGO_CONFTEST
from tests.test_misses import SETTINGS


def same(project: Project, files: dict[str, str]) -> None:
    """Check that the tests pass with and without recording."""
    project.commit(files)
    for args in ([], ["--blast-radius-record"]):
        result = project.pytester.runpytest_subprocess("-p", "no:randomly", *args)
        outcomes = result.parseoutcomes()
        outcomes.pop("warnings", None)
        assert set(outcomes) == {"passed"}, "\n".join(result.outlines[-30:])


def test_mocked_lstat(project: Project) -> None:
    same(
        project,
        {
            "data.txt": "x",
            "fresh.py": "def f():\n    return 1\n",
            # Coverage, when measuring the run, resolves paths through os.lstat too.
            "tests/test_a.py": """\
                import os
                import sys
                from unittest import mock

                real = os.lstat
                callers = []

                def spy(*args, **kwargs):
                    callers.append(sys._getframe(1).f_code.co_filename)
                    return real(*args, **kwargs)

                def test_open_and_import():
                    with mock.patch("os.lstat", spy):
                        with open("data.txt") as f:
                            f.read()
                        import fresh
                    assert fresh.f() == 1
                    assert not [c for c in callers if "pytest_blast_radius" in c]
            """,
        },
    )


def test_conftest_that_fails_to_import(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "class Held:\n    def __del__(self):\n        pass\n\n\n"
            "HELD = Held()\n",
            "conftest.py": "import pkg\n\nraise ImportError('broken')\n",
            "test_a.py": "def test_a():\n    pass\n",
        }
    )
    result = project.pytester.runpytest_subprocess("--blast-radius")
    # Recording stops before the interpreter tears its modules down.
    assert "Exception ignored" not in result.stderr.str()


def test_access_without_working_directory(project: Project) -> None:
    same(
        project,
        {
            "tests/test_a.py": """\
                import os
                import tempfile

                def test_access():
                    old = os.getcwd()
                    gone = tempfile.mkdtemp()
                    os.chdir(gone)
                    os.rmdir(gone)
                    try:
                        assert os.access("missing", os.F_OK) is False
                    finally:
                        os.chdir(old)
            """,
        },
    )


def test_classes_collected(project: Project) -> None:
    same(
        project,
        {
            "tests/test_a.py": """\
                import gc
                import weakref

                class Base:
                    pass

                def test_make():
                    class Local(Base):
                        def go(self):
                            return 1
                    assert Local().go() == 1
                    ref = weakref.ref(Local)
                    del Local
                    gc.collect()
                    assert ref() is None

                def test_registry():
                    gc.collect()
                    assert Base.__subclasses__() == []
            """,
        },
    )


def test_thread_running_new_code(project: Project) -> None:
    same(
        project,
        {
            "gen.py": (
                "def f(x):\n    y = x\n    return y\n\n\n"
                "class C:\n    def m(self):\n        return 1\n"
            ),
            "tests/conftest.py": """\
                import threading
                import types

                import gen

                def spin():
                    n = 0
                    while True:
                        n += 1
                        name = f"f{n}"
                        code = gen.f.__code__.replace(co_name=name, co_qualname=name)
                        types.FunctionType(code, {})(n)
                        type(f"C{n}", (gen.C,), {})().m()

                threading.Thread(target=spin, daemon=True).start()
            """,
            "tests/test_a.py": """\
                import pytest

                @pytest.mark.parametrize("i", range(300))
                def test_n(i):
                    pass
            """,
        },
    )


def test_django(project: Project) -> None:
    same(
        project,
        {
            "site_/__init__.py": "",
            "site_/settings.py": SETTINGS + 'STATIC_URL = "static/"\n',
            "app/__init__.py": "",
            "app/models.py": """\
                from django.db import models

                class Thing(models.Model):
                    name = models.CharField(max_length=10)
            """,
            "app/urls.py": "urlpatterns = []\n",
            "tests/__init__.py": "",
            "tests/conftest.py": DJANGO_CONFTEST,
            "tests/test_a.py": """\
                import inspect

                from django.conf import settings
                from django.urls import set_script_prefix

                from app.models import Thing

                def test_script_prefix():
                    assert settings.STATIC_URL == "/static/"
                    set_script_prefix("/sub/")
                    try:
                        assert settings.STATIC_URL == "/static/"
                    finally:
                        set_script_prefix("/")

                def test_async_method():
                    assert inspect.iscoroutinefunction(Thing.asave)
            """,
        },
    )
