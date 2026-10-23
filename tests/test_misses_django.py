"""Django changes that once made selection skip a test whose outcome they change."""

import struct
from collections.abc import Mapping

from tests.conftest import Project
from tests.test_misses import DJANGO
from tests.test_misses import DJANGO_CONFTEST
from tests.test_misses import OTHER
from tests.test_misses import SETTINGS

TWO = {"tests/test_t.py::test_a", "tests/test_t.py::test_b"}


def run(
    project: Project,
    files: dict[str, str],
    changes: Mapping[str, str | bytes],
    passed: int,
) -> set[str]:
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=passed)
    for name, content in changes.items():
        path = project.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return project.ran("--blast-radius")


def mo(messages: dict[str, str]) -> bytes:
    """Return a compiled gettext catalog."""
    messages = {"": "Content-Type: text/plain; charset=UTF-8\n", **messages}
    keys = sorted(messages)
    ids = strs = b""
    offsets = []
    for key in keys:
        value = messages[key].encode()
        offsets.append((len(ids), len(key.encode()), len(strs), len(value)))
        ids += key.encode() + b"\0"
        strs += value + b"\0"
    keystart = 7 * 4 + 16 * len(keys)
    valuestart = keystart + len(ids)
    koffsets: list[int] = []
    voffsets: list[int] = []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    header = struct.pack(
        "Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0
    )
    return (
        header
        + struct.pack(f"{len(koffsets)}i", *koffsets)
        + struct.pack(f"{len(voffsets)}i", *voffsets)
        + ids
        + strs
    )


CATALOG = "app/locale/nl/LC_MESSAGES/django.mo"


def test_mo_catalog_cached(project: Project) -> None:
    hallo = "    with translation.override('nl'):\n"
    hallo += "        assert translation.gettext('hello') == 'hallo'\n"
    plural = "        assert translation.ngettext('hello', 'hellos', 2)\n"
    tests = (
        "from django.utils import translation\n\n\n"
        f"def test_a():\n{hallo}\n\ndef test_b():\n{hallo}{plural}" + OTHER
    )
    (project.path / CATALOG).parent.mkdir(parents=True)
    (project.path / CATALOG).write_bytes(mo({"hello": "hallo"}))
    ran = run(
        project,
        {**DJANGO, "tests/test_t.py": tests},
        {CATALOG: mo({"hello": "hoi"})},
        3,
    )
    # The second test reads the catalog the first one loaded; the third, none.
    assert ran == TWO


def test_mo_catalog_rebuilt_from_gettext_cache(project: Project) -> None:
    # Changing LANGUAGES drops Django's catalogs, but not the messages gettext keeps.
    hallo = "    with translation.override('nl'):\n"
    hallo += "        assert translation.gettext('hello') == 'hallo'\n"
    tests = (
        "from django.test import override_settings\n"
        "from django.utils import translation\n\n\n"
        f"def test_a():\n{hallo}\n\n"
        "@override_settings(LANGUAGES=[('nl', 'Dutch'), ('en', 'English')])\n"
        f"def test_b():\n{hallo}" + OTHER
    )
    (project.path / CATALOG).parent.mkdir(parents=True)
    (project.path / CATALOG).write_bytes(mo({"hello": "hallo"}))
    ran = run(
        project,
        {**DJANGO, "tests/test_t.py": tests},
        {CATALOG: mo({"hello": "hoi"})},
        3,
    )
    assert ran == TWO


def test_locale_added(project: Project) -> None:
    tests = (
        "from django.utils import translation\n\n\n"
        "def test_a():\n    with translation.override('de'):\n"
        "        assert translation.gettext('hello') == 'hello'\n" + OTHER
    )
    (project.path / CATALOG).parent.mkdir(parents=True)
    (project.path / CATALOG).write_bytes(mo({"hello": "hallo"}))
    ran = run(
        project,
        {**DJANGO, "tests/test_t.py": tests},
        {"app/locale/de/LC_MESSAGES/django.mo": mo({"hello": "hallo"})},
        2,
    )
    assert "tests/test_t.py::test_a" in ran


# A test reading a value another test stored is credited with what computed it.
def test_fragment_cache(project: Project) -> None:
    template = "{% load cache %}\n{% cache 500 side %}\nhello\n{% endcache %}\n"
    render = "    assert render_to_string('c.html').split() == ['hello']\n"
    tests = (
        "from django.template.loader import render_to_string\n\n\n"
        f"def test_a():\n{render}\n\ndef test_b():\n{render}" + OTHER
    )
    files = {**DJANGO, "app/templates/c.html": template, "tests/test_t.py": tests}
    ran = run(
        project, files, {"app/templates/c.html": template.replace("hello", "bye")}, 3
    )
    assert ran >= TWO


def test_cache_page(project: Project) -> None:
    views = (
        "from django.http import HttpResponse\n"
        "from django.views.decorators.cache import cache_page\n\n\n"
        "@cache_page(60)\ndef x(request):\n    return HttpResponse('hello')\n"
    )
    get = "    assert Client().get('/x/').content == b'hello'\n"
    tests = (
        "from django.test import Client\n\n\n"
        f"def test_a():\n{get}\n\ndef test_b():\n{get}" + OTHER
    )
    files = {
        **DJANGO,
        "app/views.py": views,
        "app/urls.py": (
            "from django.urls import path\n\nfrom app import views\n\n"
            "urlpatterns = [path('x/', views.x)]\n"
        ),
        "tests/test_t.py": tests,
    }
    ran = run(project, files, {"app/views.py": views.replace("'hello'", "'bye'")}, 3)
    assert ran >= TWO


def test_low_level_cache(project: Project) -> None:
    stats = (
        "from django.core.cache import cache\n\n\n"
        "def compute():\n    return 1\n\n\n"
        "def stats():\n    return cache.get_or_set('stats', compute)\n"
    )
    get = "    assert stats() == 1\n"
    tests = (
        "from app.stats import stats\n\n\n"
        f"def test_a():\n{get}\n\ndef test_b():\n{get}" + OTHER
    )
    files = {**DJANGO, "app/stats.py": stats, "tests/test_t.py": tests}
    ran = run(project, files, {"app/stats.py": stats.replace("1\n\n", "2\n\n", 1)}, 3)
    assert ran >= TWO


def test_format_module_path_setting(project: Project) -> None:
    date = "datetime.date(2020, 1, 2)"
    check = f"    assert formats.date_format({date}) == 'Jan. 2, 2020'\n"
    tests = (
        "import datetime\n\nfrom django.utils import formats\n\n\n"
        f"def test_a():\n{check}\n\ndef test_b():\n{check}" + OTHER
    )
    files = {
        **DJANGO,
        "app/formats/__init__.py": "",
        "app/formats/en/__init__.py": "",
        "app/formats/en/formats.py": "DATE_FORMAT = 'Y-m-d'\n",
        "tests/test_t.py": tests,
    }
    new = SETTINGS + "FORMAT_MODULE_PATH = 'app.formats'\n"
    assert run(project, files, {"site_/settings.py": new}, 3) >= TWO


FEATURE_TESTS = (
    "from app.feature import enabled\n\n\n"
    "def test_a():\n    assert enabled() is False\n" + OTHER
)


def test_missing_setting_added(project: Project) -> None:
    files = {
        **DJANGO,
        "app/feature.py": (
            "from django.conf import settings\n\n\n"
            "def enabled():\n    return getattr(settings, 'FEATURE', False)\n"
        ),
        "tests/test_t.py": FEATURE_TESTS,
    }
    ran = run(project, files, {"site_/settings.py": SETTINGS + "FEATURE = True\n"}, 2)
    assert "tests/test_t.py::test_a" in ran


def test_setting_from_star_imported_module(project: Project) -> None:
    files = {
        **DJANGO,
        "site_/settings.py": SETTINGS + "from site_.common import *  # noqa\n",
        "site_/common.py": "FEATURE = False\n",
        "app/feature.py": (
            "from django.conf import settings\n\n\n"
            "def enabled():\n    return settings.FEATURE\n"
        ),
        "tests/test_t.py": FEATURE_TESTS,
    }
    ran = run(project, files, {"site_/common.py": "FEATURE = True\n"}, 2)
    assert "tests/test_t.py::test_a" in ran


def test_setting_read_by_module_loaded_in_test(project: Project) -> None:
    views = (
        "from django.conf import settings\nfrom django.http import HttpResponse\n\n"
        "PAGE = settings.PAGE\n\n\n"
        "def x(request):\n    return HttpResponse(str(PAGE))\n"
    )
    get = "    assert Client().get('/x/').content == b'1'\n"
    tests = (
        "from django.test import Client\n\n\n"
        f"def test_a():\n{get}\n\ndef test_b():\n{get}" + OTHER
    )
    files = {
        **DJANGO,
        "site_/settings.py": SETTINGS + "PAGE = 1\n",
        "app/views.py": views,
        "app/urls.py": (
            "from django.urls import path\n\nfrom app import views\n\n"
            "urlpatterns = [path('x/', views.x)]\n"
        ),
        "tests/test_t.py": tests,
    }
    assert run(project, files, {"site_/settings.py": SETTINGS + "PAGE = 2\n"}, 3) >= TWO


DB_SETTINGS = (
    SETTINGS + "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', "
    "'NAME': ':memory:'}}\nDEFAULT_AUTO_FIELD = 'django.db.models.AutoField'\n"
)
DB_CONFTEST = (
    DJANGO_CONFTEST
    + "\nfrom django.db import connection\n\nfrom app.models import A\n\n"
    "with connection.schema_editor() as editor:\n    editor.create_model(A)\n"
)
CHOICE_MODELS = (
    "from django.db import models\n\n\n"
    "class Status(models.TextChoices):\n    NEW = 'n', 'New'\n\n\n"
    "class A(models.Model):\n"
    "    status = models.CharField(max_length=1, choices=Status)\n"
)


def test_choices_label(project: Project) -> None:
    files = {
        **DJANGO,
        "site_/settings.py": DB_SETTINGS,
        "tests/conftest.py": DB_CONFTEST,
        "app/models.py": CHOICE_MODELS,
        "tests/test_t.py": (
            "from app.models import A\n\n\n"
            "def test_a():\n    assert A(status='n').get_status_display() == 'New'\n"
            + OTHER
        ),
    }
    new = CHOICE_MODELS.replace("'New'", "'Fresh'")
    assert "tests/test_t.py::test_a" in run(project, files, {"app/models.py": new}, 2)
