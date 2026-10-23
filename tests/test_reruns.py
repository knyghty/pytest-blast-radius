"""Failures that come from a module that didn't load run again once it might."""

from pytest_blast_radius import selection
from tests.conftest import Project
from tests.test_misses import DJANGO
from tests.test_misses import OTHER

URLS = (
    "from django.urls import path\n\nfrom app import views\n\n"
    "urlpatterns = [path('x/', views.x), path('y/', views.y)]\n"
)
ROOT = (
    "from django.urls import include, path\n\n"
    "urlpatterns = [path('', include('app.sub'))]\n"
)
VIEWS = (
    "from django.http import HttpResponse\n\n\n"
    "def x(request):\n    return HttpResponse('x')\n"
)


def test_included_urlconf_that_failed_to_load(project: Project) -> None:
    project.commit(
        {
            **DJANGO,
            "app/urls.py": ROOT,
            "app/sub.py": URLS,
            "app/views.py": VIEWS,
            # Other code imports the views, but not the failing test's.
            "tests/test_v.py": (
                "from app import views\n\n\ndef test_v():\n    assert views.x\n"
            ),
            "tests/test_t.py": (
                "from django.test import Client\n\n\n"
                "def test_a():\n    assert Client().get('/x/').content == b'x'\n"
                + OTHER
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(failed=1, passed=2)
    project.write(
        {"app/views.py": VIEWS + "\n\ndef y(request):\n    return HttpResponse('y')\n"}
    )
    result = project.run("--blast-radius")
    result.assert_outcomes(passed=1)
    assert "reused" not in result.stdout.str()


def test_urlconf_failure_a_template_swallowed(project: Project) -> None:
    project.commit(
        {
            **DJANGO,
            "app/urls.py": ROOT,
            "app/sub.py": URLS.replace("views.x)", "views.x, name='x')"),
            "app/views.py": VIEWS,
            "app/templatetags/__init__.py": "",
            "app/templatetags/links.py": (
                "from django import template\nfrom django.urls import reverse\n\n"
                "register = template.Library()\n\n\n@register.simple_tag\n"
                "def link():\n    try:\n        return reverse('x')\n"
                "    except Exception:\n        return ''\n"
            ),
            "app/templates/l.html": "{% load links %}{% link %}",
            "tests/test_v.py": (
                "from app import views\n\n\ndef test_v():\n    assert views.x\n"
            ),
            "tests/test_t.py": (
                "from django.template.loader import render_to_string\n\n\n"
                "def test_a():\n    assert render_to_string('l.html') == '/x/'\n"
                + OTHER
            ),
        }
    )
    project.run("--blast-radius").assert_outcomes(failed=1, passed=2)
    project.write(
        {"app/views.py": VIEWS + "\n\ndef y(request):\n    return HttpResponse('y')\n"}
    )
    result = project.run("--blast-radius")
    result.assert_outcomes(passed=1)
    assert "reused" not in result.stdout.str()


def test_reaching_through_traceback_files(project: Project) -> None:
    project.commit(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "X = 1\n",
            "tests/test_t.py": "def test_a():\n    assert False\n",
        }
    )
    project.run("--blast-radius").assert_outcomes(failed=1)
    [data] = project.maps()
    selector = selection.Selector(data, [], [])
    nodeid = "tests/test_t.py::test_a"
    sources = {"pkg/broken.py": "def (", "pkg/other.py": "import json\n"}
    reached = {
        failed_in: selector.reaching(
            [nodeid], ["pkg/m.py"], {nodeid: [failed_in]}, sources.get
        )
        for failed_in in ["pkg/m.py", "pkg/broken.py", "pkg/other.py"]
    }
    # The changed file itself, and one that no longer parses, may reach anything.
    assert reached == {
        "pkg/m.py": {nodeid},
        "pkg/broken.py": {nodeid},
        "pkg/other.py": set(),
    }
