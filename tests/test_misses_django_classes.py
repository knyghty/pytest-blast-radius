"""Django classes Django finds methods on, or that project classes hold."""

from tests.conftest import Project
from tests.test_misses import DJANGO
from tests.test_misses import OTHER
from tests.test_misses import SETTINGS
from tests.test_misses_django import DB_CONFTEST
from tests.test_misses_django import DB_SETTINGS
from tests.test_misses_django import run

A = "tests/test_t.py::test_a"
MIDDLEWARE = (
    "class M:\n"
    "    def __init__(self, get_response):\n"
    "        self.get_response = get_response\n\n"
    "    def __call__(self, request):\n"
    "        return self.get_response(request)\n"
)


def test_middleware_hook_added(project: Project) -> None:
    files = {
        **DJANGO,
        "site_/settings.py": SETTINGS.replace(
            "MIDDLEWARE = []", "MIDDLEWARE = ['app.middleware.M']"
        ),
        "app/middleware.py": MIDDLEWARE,
        "app/views.py": (
            "from django.http import HttpResponse\n\n\n"
            "def x(request, **kwargs):\n    return HttpResponse('hello')\n"
        ),
        "app/urls.py": (
            "from django.urls import path\n\nfrom app import views\n\n"
            "urlpatterns = [path('x/', views.x)]\n"
        ),
        "tests/test_t.py": (
            "from django.test import Client\n\n\n"
            "def test_a():\n    assert Client().get('/x/').content == b'hello'\n"
            + OTHER
        ),
    }
    new = MIDDLEWARE + (
        "\n    def process_view(self, request, view, args, kwargs):\n"
        "        from django.http import HttpResponse\n\n"
        "        return HttpResponse('blocked')\n"
    )
    assert A in run(project, files, {"app/middleware.py": new}, 2)


FIELD_MODELS = (
    "from django.db import models\n\n\n"
    "class Upper(models.CharField):\n"
    "    def describe(self):\n        return 'upper'\n\n\n"
    "class A(models.Model):\n    name = Upper(max_length=10)\n"
)


def test_model_field_converter_added(project: Project) -> None:
    files = {
        **DJANGO,
        "site_/settings.py": DB_SETTINGS,
        "tests/conftest.py": DB_CONFTEST,
        "app/models.py": FIELD_MODELS,
        "tests/test_t.py": (
            "from app.models import A\n\n\n"
            "def test_a():\n    A.objects.create(name='x')\n"
            "    assert A.objects.get().name == 'x'\n" + OTHER
        ),
    }
    new = FIELD_MODELS.replace(
        "    def describe",
        "    def from_db_value(self, value, expression, connection):\n"
        "        return value.upper()\n\n    def describe",
    )
    assert A in run(project, files, {"app/models.py": new}, 2)


MANAGER_MODELS = (
    "from django.db import models\n\n\n"
    "class Manager(models.Manager):\n"
    "    def named(self):\n        return self.filter(name='x')\n\n\n"
    "class A(models.Model):\n    name = models.CharField(max_length=10)\n"
    "    objects = Manager()\n"
)


def test_manager_get_queryset_added(project: Project) -> None:
    files = {
        **DJANGO,
        "site_/settings.py": DB_SETTINGS,
        "tests/conftest.py": DB_CONFTEST,
        "app/models.py": MANAGER_MODELS,
        "tests/test_t.py": (
            "from app.models import A\n\n\n"
            "def test_a():\n    A.objects.create(name='y')\n"
            "    assert A.objects.count() == 1\n" + OTHER
        ),
    }
    new = MANAGER_MODELS.replace(
        "    def named",
        "    def get_queryset(self):\n"
        "        return super().get_queryset().filter(name='x')\n\n    def named",
    )
    assert A in run(project, files, {"app/models.py": new}, 2)


WIDGET_FORMS = (
    "from django import forms\n\n\n"
    "class W(forms.TextInput):\n    input_type = 'text'\n\n\n"
    "class F(forms.Form):\n    name = forms.CharField(widget=W)\n"
)


def test_widget_attribute(project: Project) -> None:
    files = {
        **DJANGO,
        "app/forms.py": WIDGET_FORMS,
        "tests/test_t.py": (
            "from app.forms import F\n\n\n"
            "def test_a():\n    assert 'type=\"text\"' in str(F()['name'])\n" + OTHER
        ),
    }
    new = WIDGET_FORMS.replace("'text'", "'email'")
    assert A in run(project, files, {"app/forms.py": new}, 2)


FIELD_FORMS = (
    "from django import forms\n\n\n"
    "class Code(forms.CharField):\n"
    "    def describe(self):\n        return 'code'\n\n\n"
    "class F(forms.Form):\n    code = Code()\n"
)


def test_form_field_override_added(project: Project) -> None:
    files = {
        **DJANGO,
        "app/forms.py": FIELD_FORMS,
        "tests/test_t.py": (
            "from app.forms import F\n\n\n"
            "def test_a():\n    f = F({'code': 'ab'})\n"
            "    assert f.is_valid() and f.cleaned_data['code'] == 'ab'\n" + OTHER
        ),
    }
    new = FIELD_FORMS.replace(
        "    def describe",
        "    def to_python(self, value):\n        return value.upper()\n\n"
        "    def describe",
    )
    assert A in run(project, files, {"app/forms.py": new}, 2)


FACTORIES = (
    "import factory\n\n\nclass AFactory(factory.Factory):\n"
    "    class Meta:\n        model = dict\n\n    name = 'x'\n\n\n"
    "class BFactory(factory.Factory):\n"
    "    class Meta:\n        model = dict\n\n    a = factory.SubFactory(AFactory)\n"
)


def test_factory_declaration_changed(project: Project) -> None:
    files = {
        **DJANGO,
        "app/factories.py": FACTORIES,
        "tests/test_t.py": (
            "from app.factories import AFactory\n"
            "from app.factories import BFactory\n\n\n"
            "def test_a():\n    assert AFactory()['name'] == 'x'\n\n\n"
            "def test_b():\n    assert BFactory()['a']['name'] == 'x'\n\n\n"
            "def test_c():\n    assert BFactory\n"
        ),
    }
    changes = {"app/factories.py": FACTORIES.replace("'x'", "'y'")}
    assert run(project, files, changes, 3) == {A, "tests/test_t.py::test_b"}


# As Wagtail blocks held in blocks held in a model's StreamField, which only
# deconstructing the field reads.
SPEC = "class Inner:\n    min_num = 2\n\n\nclass Outer:\n    inner = Inner()\n"
HELD = (
    "from django.db import models\n\nfrom app import specs\n\n\n"
    "class SpecField(models.CharField):\n"
    "    def __init__(self, *args, spec=None, min_num=None, **kwargs):\n"
    "        self.spec, self.min_num = spec, min_num\n"
    "        super().__init__(*args, **kwargs)\n\n"
    "    def deconstruct(self):\n"
    "        name, path, args, kwargs = super().deconstruct()\n"
    "        number = self.spec.inner.min_num if self.spec else self.min_num\n"
    "        return name, path, args, {**kwargs, 'min_num': number}\n\n\n"
    "class A(models.Model):\n    f = SpecField(max_length=10, spec=specs.Outer())\n"
)


def test_class_held_by_a_model_migrations_read(project: Project) -> None:
    # As makemigrations --check does, it reads every model, without naming one.
    files = {
        **DJANGO,
        "app/models.py": HELD,
        "app/specs.py": SPEC,
        "tests/test_t.py": (
            "from django.apps import apps\n"
            "from django.db.migrations.state import ModelState\n\n\n"
            "def test_a():\n    assert [\n"
            "        ModelState.from_model(model).fields['f'].deconstruct()[3]"
            "['min_num']\n"
            "        for model in apps.get_app_config('app').get_models()\n"
            "    ] == [2]\n" + OTHER
        ),
    }
    changes = {"app/specs.py": SPEC.replace("min_num = 2", "min_num = 3")}
    assert A in run(project, files, changes, 2)


RECEIVERS = (
    "from django.db.models.signals import pre_save\n"
    "from django.dispatch import receiver\n\nfrom app.models import B\n\n\n"
    "@receiver(pre_save, sender=B)\n"
    "def shout(sender, instance, **kwargs):\n"
    "    instance.name = instance.name.upper()\n"
)


def test_receiver_sender_changed(project: Project) -> None:
    files = {
        **DJANGO,
        "site_/settings.py": DB_SETTINGS.replace(
            '"app"', '"app.apps.AppConfig_"'
        ).replace("'app'", "'app.apps.AppConfig_'"),
        "tests/conftest.py": DB_CONFTEST,
        "app/apps.py": (
            "from django.apps import AppConfig\n\n\n"
            "class AppConfig_(AppConfig):\n    name = 'app'\n\n"
            "    def ready(self):\n        from app import receivers  # noqa\n"
        ),
        "app/models.py": (
            "from django.db import models\n\n\n"
            "class A(models.Model):\n    name = models.CharField(max_length=10)\n\n\n"
            "class B(models.Model):\n    name = models.CharField(max_length=10)\n"
        ),
        "app/receivers.py": RECEIVERS,
        "tests/test_t.py": (
            "from app.models import A\n\n\n"
            "def test_a():\n    a = A.objects.create(name='x')\n"
            "    assert a.name == 'x'\n" + OTHER
        ),
    }
    new = RECEIVERS.replace("import B", "import A").replace("sender=B", "sender=A")
    assert A in run(project, files, {"app/receivers.py": new}, 2)
