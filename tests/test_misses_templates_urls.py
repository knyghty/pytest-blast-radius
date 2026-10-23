"""Template and URL changes outside what the recorded run rendered or reversed."""

from tests.conftest import Project
from tests.test_misses import DJANGO
from tests.test_misses import OTHER
from tests.test_misses_django import run

A = "tests/test_t.py::test_a"


def test_load_added_in_branch_not_rendered(project: Project) -> None:
    template = "{% if show %}\nx\n{% endif %}\n{{ 'a'|upper }}"
    files = {
        **DJANGO,
        "app/templatetags/__init__.py": "",
        "app/templatetags/lib.py": (
            "from django import template\n\nregister = template.Library()\n\n\n"
            "@register.filter\ndef upper(value):\n    return 'X'\n"
        ),
        "app/templates/l.html": template,
        "tests/test_t.py": (
            "from django.template.loader import render_to_string\n\n\n"
            "def test_a():\n    assert render_to_string('l.html').strip() == 'A'\n"
            + OTHER
        ),
    }
    changes = {"app/templates/l.html": template.replace("\nx\n", "\n{% load lib %}x\n")}
    assert A in run(project, files, changes, 2)


URLS = (
    "from django.urls import path\n\nfrom app import views\n\n"
    "urlpatterns = [path('x/', views.x, name='x')]\n"
)


def test_reverse_as_test_module_loads(project: Project) -> None:
    files = {
        **DJANGO,
        "app/views.py": "def x(request):\n    pass\n",
        "app/urls.py": URLS,
        "tests/test_t.py": (
            "from django.urls import reverse\n\nURL = reverse('x')\n\n\n"
            "def test_a():\n    assert URL == '/x/'\n" + OTHER
        ),
    }
    assert A in run(project, files, {"app/urls.py": URLS.replace("'x/'", "'y/'")}, 2)


def test_name_added_to_earlier_include(project: Project) -> None:
    sub = (
        "from django.urls import path\n\nfrom app import views\n\nurlpatterns = [{}]\n"
    )
    files = {
        **DJANGO,
        "app/views.py": "def x(request):\n    pass\n",
        "app/urls.py": (
            "from django.urls import include, path\n\nurlpatterns = ["
            "path('b/', include('app.b')), path('a/', include('app.a'))]\n"
        ),
        "app/a.py": sub.format("path('o/', views.x, name='other')"),
        "app/b.py": sub.format("path('x/', views.x, name='x')"),
        "tests/test_t.py": (
            "from django.urls import reverse\n\n\n"
            "def test_a():\n    assert reverse('x') == '/b/x/'\n" + OTHER
        ),
    }
    changes = {
        "app/a.py": sub.format(
            "path('o/', views.x, name='other'), path('x/', views.x, name='x')"
        )
    }
    assert A in run(project, files, changes, 2)
