"""Credit tests with the Django classes, URLconfs and commands they use.

Django reads these classes' attributes itself, and loads URLconfs, commands and
template tag libraries by name, so a test can depend on them without running any
of their code.
"""

import functools
import gettext as gettext_module
import inspect
import sys
from collections.abc import Callable
from collections.abc import Hashable
from typing import Any
from typing import cast

from django.apps import apps
from django.conf import LazySettings
from django.conf import settings
from django.core import management
from django.core.cache.backends.locmem import LocMemCache
from django.core.handlers.base import BaseHandler
from django.db.migrations.state import ModelState
from django.db.models import Model
from django.db.models import QuerySet
from django.db.models import signals
from django.forms.forms import BaseForm
from django.forms.formsets import BaseFormSet
from django.forms.models import BaseModelForm
from django.template import engines
from django.template.base import Node
from django.template.base import TextNode
from django.template.base import TokenType
from django.template.loaders import cached
from django.test.signals import template_rendered
from django.urls import URLResolver
from django.urls import get_resolver
from django.urls import get_urlconf
from django.utils.translation.trans_real import DjangoTranslation
from django.views import generic
from django.views.generic.base import View

from pytest_blast_radius import templates
from pytest_blast_radius.recording import STATE
from pytest_blast_radius.recording import identity

# Special methods projects override that Django's classes define.
DUNDERS = {"__init__", "__str__"}


def _model(
    _: object, model: type | None = None, *args: object, **kwargs: object
) -> type | None:
    return model


def _own_class(instance: object, *args: object, **kwargs: object) -> type:
    return type(instance)


def _admin_installed() -> bool:
    return apps.ready and apps.is_installed("django.contrib.admin")


def _factory(step: Any, *args: object, **kwargs: object) -> type:  # noqa: ANN401
    return step.factory_meta.factory


def _factory_classes() -> tuple[type | None, type | None]:
    """Return factory_boy's base factory and the builder every factory, including a
    SubFactory, builds through, if it is installed."""
    try:
        from factory.base import BaseFactory  # noqa: PLC0415
        from factory.builder import StepBuilder  # noqa: PLC0415
    except ImportError:
        return None, None
    return BaseFactory, StepBuilder


def patches() -> list[tuple[type, str, Callable[..., Any]]]:
    """Return the methods to wrap, and how to find the class each call uses."""
    result: list[tuple[type, str, Callable[..., Any]]] = [
        (QuerySet, "__init__", _model),
        (BaseForm, "__init__", _own_class),
        (BaseFormSet, "__init__", _own_class),
        (View, "setup", _own_class),
    ]
    if _admin_installed():
        from django.contrib.admin.options import BaseModelAdmin  # noqa: PLC0415
        from django.contrib.admin.options import InlineModelAdmin  # noqa: PLC0415
        from django.contrib.admin.options import ModelAdmin  # noqa: PLC0415

        result += [
            (BaseModelAdmin, "get_queryset", _own_class),
            (ModelAdmin, "get_form", _own_class),
            (InlineModelAdmin, "get_formset", _own_class),
        ]
    # factory_boy reads a factory's declarations itself as it builds.
    if (builder := _factory_classes()[1]) is not None:
        result.append((builder, "build", _factory))
    return result


def overridable() -> list[tuple[type, str]]:
    """Return the public methods of Django's model, form, view and admin classes.

    Project classes override these and Django calls them, often without project
    code mentioning them, as with a model's `save`.
    """
    roots: list[type] = [Model, BaseModelForm, BaseFormSet, *_generic_views()]
    if _admin_installed():
        from django.contrib.admin import options  # noqa: PLC0415

        roots += [options.ModelAdmin, options.StackedInline, options.TabularInline]
    return sorted(
        {
            (owner, name)
            for root in roots
            for owner in root.__mro__
            if owner is not object
            for name, value in vars(owner).items()
            if inspect.isfunction(value)
            and (not name.startswith("_") or name in DUNDERS)
        },
        key=lambda pair: (pair[0].__module__, pair[0].__qualname__, pair[1]),
    )


def _generic_views() -> list[type]:
    return [
        value
        for value in vars(generic).values()
        if isinstance(value, type) and issubclass(value, View)
    ]


def _call_wrap(
    original: Callable[..., Any], name: str, credit_call: Callable[[type, str], None]
) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(self: object, *args: object, **kwargs: object) -> object:
        credit_call(type(self), name)
        return original(self, *args, **kwargs)

    # Code such as asgiref checks whether a method is async, as `asave` is.
    if inspect.iscoroutinefunction(original):
        inspect.markcoroutinefunction(wrapper)
    wrapper.blast_radius_calls = True  # ty: ignore[unresolved-attribute]
    return wrapper


def settings_file() -> str | None:
    """Return the settings module's file, if settings come from one."""
    module = getattr(settings._wrapped, "SETTINGS_MODULE", None)
    return getattr(sys.modules.get(module or ""), "__file__", None)


def _settings_patches(
    use_module: Callable[[str, str], None],
) -> list[tuple[object, str, object]]:
    """Return what to wrap to credit each test with the settings it reads."""
    file = settings_file()
    if file is None:
        return []
    get = LazySettings.__getattr__

    @functools.wraps(get)
    def getattr_wrapper(self: LazySettings, name: str) -> object:
        # A read of a missing setting counts too: adding it changes the result.
        use_module(file, f"setting:{name}")
        value = get(self, name)
        # A cached setting skips __getattr__, so later reads would go unseen. Django
        # adds the script prefix to these as it caches them, so they stay.
        if name not in {"MEDIA_URL", "STATIC_URL"}:
            self.__dict__.pop(name, None)
        return value

    return [(LazySettings, "__getattr__", getattr_wrapper)]


def _render_wrap(
    render: Callable[..., Any], render_line: Callable[[str, int], None]
) -> Callable[..., Any]:
    @functools.wraps(render)
    def wrapper(self: Node, context: object) -> object:
        token = getattr(self, "token", None)
        origin = getattr(self, "origin", None)
        if token is not None and origin is not None:
            text = token.token_type == TokenType.TEXT
            line = templates.node_line(token.lineno, token.contents, text=text)
            render_line(str(origin.name), line)
        return render(self, context)

    return wrapper


def covered() -> tuple[type, ...]:
    """Return the bases of the classes the hooks credit wherever tests use them."""
    bases: tuple[type, ...] = (Model, BaseForm, BaseFormSet, View)
    if _admin_installed():
        from django.contrib.admin.options import BaseModelAdmin  # noqa: PLC0415

        bases += (BaseModelAdmin,)
    if (factory := _factory_classes()[0]) is not None:
        bases += (factory,)
    return bases


def _wrap(
    original: Callable[..., Any],
    subject: Callable[..., Any],
    credit: Callable[[type | None], None],
) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(self: object, *args: object, **kwargs: object) -> object:
        credit(subject(self, *args, **kwargs))
        return original(self, *args, **kwargs)

    return wrapper


def _module_file(resolver: URLResolver) -> str | None:
    return getattr(resolver.urlconf_module, "__file__", None)


@functools.lru_cache(maxsize=16)
def _url_modules(root: URLResolver) -> tuple[dict[int, str], set[str]]:
    """Return the URL modules under a root URLconf, by their patterns' ids, and
    the modules that include a namespace.

    Namespaced reversing wraps a module's patterns in resolvers of its own, which
    only the patterns tie back to the module. It finds the namespace, and the
    prefixes before it, in the modules that include one. Django caches root
    resolvers, and makes new ones when the URLconf changes.
    """
    files = {}
    namespacing = set()
    pending: list[tuple[URLResolver, str | None]] = [(root, None)]
    while pending:
        resolver, file = pending.pop()
        if (own := _module_file(resolver)) is not None:
            files[id(resolver.url_patterns)] = file = own
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                if pattern.namespace and file is not None:
                    namespacing.add(file)
                pending.append((pattern, file))
    return files, namespacing


def reversed_from(resolver: URLResolver, name: object) -> set[tuple[str, str]]:
    """Return the URL modules that may change what a resolver reverses a name to.

    Each comes with what in it matters: "name:<name>" for the name or the includes
    on the way to it, "namespaces:" for the includes that give namespaces, or ""
    for anything in it. A resolver reverses names from its patterns and from
    includes without a namespace, so the modules on the way to the name count,
    for their prefixes. Any of them may come to define the name, first or at all,
    as code may catch NoReverseMatch, and a view rather than a name from anything
    in them. A namespaced reverse also depends on the modules that include
    namespaces.
    """
    files, namespacing = _url_modules(get_resolver(get_urlconf()))
    every: set[str] = set()
    pending: list[tuple[URLResolver, tuple[str, ...]]] = [(resolver, ())]
    while pending:
        current, chain = pending.pop()
        file = _module_file(current) or files.get(id(current.url_patterns))
        if file is not None:
            chain = (*chain, file)
        every.update(chain)
        pending.extend(
            (pattern, chain)
            for pattern in current.url_patterns
            if isinstance(pattern, URLResolver) and not pattern.namespace
        )
    if isinstance(name, str):
        result = {(file, f"name:{name}") for file in every}
    else:
        result = {(file, "") for file in every}
    if _module_file(resolver) is None:
        result |= {(file, "namespaces:") for file in namespacing}
    return result


def template_libraries() -> tuple[dict[str, list[str]], list[str]]:
    """Return the names templates load each tag library module by, and the modules
    every template loads."""
    libraries: dict[str, list[str]] = {}
    builtins = []
    # A project may import Django without configuring it.
    if not settings.configured:
        return libraries, builtins
    # Only Django's own engine has tag libraries.
    for backend in engines.all():
        engine = getattr(backend, "engine", None)
        for name, module in getattr(engine, "libraries", {}).items():
            libraries.setdefault(module, []).append(name)
        builtins += getattr(engine, "builtins", [])
    return libraries, builtins


def _root_urlconf_file() -> str | None:
    """Return the root URLconf's file, if it has loaded."""
    name = getattr(settings, "ROOT_URLCONF", None) if settings.configured else None
    return getattr(sys.modules.get(name), "__file__", None)


def _failed_through(
    error: BaseException, use_module: Callable[[str, str], None]
) -> None:
    """Credit the files a URLconf that failed to load went through, as one naming a
    view that doesn't exist yet, since code such as a template tag may swallow the
    error, leaving no failure to trace."""
    traceback = error.__traceback__
    while traceback is not None:
        use_module(traceback.tb_frame.f_code.co_filename, "")
        traceback = traceback.tb_next


def _url_patches(
    use_module: Callable[[str, str], None],
) -> list[tuple[object, str, object]]:
    """Return what to wrap to report the URL modules each test depends on.

    Resolving a path depends on the patterns it goes through, reversing a name on
    the patterns on the way to it and on namespaces, and handling any request on
    every URLconf loading, which Django does at once.
    """
    resolve = URLResolver.resolve
    reverse = URLResolver._reverse_with_prefix
    get_response = BaseHandler.get_response
    get_response_async = BaseHandler.get_response_async
    # The URL modules being resolved through, for the lists they include.
    resolving: list[str | None] = []

    def failed(error: BaseException) -> None:
        _failed_through(error, use_module)

    @functools.wraps(resolve)
    def resolve_wrapper(self: URLResolver, path: str) -> object:
        try:
            file = _module_file(self) or (resolving[-1] if resolving else None)
        except Exception as error:
            failed(error)
            raise
        # A URLconf's patterns only see paths its include's prefix matches, and
        # only the rest of them.
        match = self.pattern.match(str(path))
        if file is not None and match:
            use_module(file, f"path:{match[0]}")
        resolving.append(file)
        try:
            return resolve(self, path)
        finally:
            resolving.pop()

    @functools.wraps(reverse)
    def reverse_wrapper(
        self: URLResolver, lookup_view: object, *args: object, **kwargs: object
    ) -> object:
        try:
            uses = reversed_from(self, lookup_view)
        except Exception as error:
            failed(error)
            raise
        for file, detail in uses:
            use_module(file, detail)
        return reverse(self, lookup_view, *args, **kwargs)

    def namespaces(prop: property) -> property:
        """Wrap a property reverse() reads namespaces from."""
        getter = cast("Callable[[URLResolver], object]", prop.fget)

        @functools.wraps(getter)
        def get(self: URLResolver) -> object:
            if (file := _module_file(self)) is not None:
                use_module(file, "namespaces:")
            return getter(self)

        return property(get)

    def requested() -> None:
        if (file := _root_urlconf_file()) is not None:
            use_module(file, "request:")

    @functools.wraps(get_response)
    def get_response_wrapper(self: BaseHandler, request: object) -> object:
        try:
            return get_response(self, request)
        finally:
            requested()

    @functools.wraps(get_response_async)
    async def get_response_async_wrapper(self: BaseHandler, request: object) -> object:
        try:
            return await get_response_async(self, request)
        finally:
            requested()

    return [
        (URLResolver, "resolve", resolve_wrapper),
        (URLResolver, "_reverse_with_prefix", reverse_wrapper),
        (URLResolver, "app_dict", namespaces(URLResolver.__dict__["app_dict"])),
        (
            URLResolver,
            "namespace_dict",
            namespaces(URLResolver.__dict__["namespace_dict"]),
        ),
        (BaseHandler, "get_response", get_response_wrapper),
        (BaseHandler, "get_response_async", get_response_async_wrapper),
    ]


def _store_patches(
    store: Callable[[str, Hashable], None],
) -> list[tuple[object, str, object]]:
    """Return what to wrap to report the local memory cache's reads and writes, so
    a test reading a value another test stored is credited with what computed it.
    A value is known by the cache's storage and its full key."""
    get, has_key, set_ = LocMemCache.get, LocMemCache.has_key, LocMemCache._set

    def read(cache: LocMemCache, key: object, version: int | None) -> None:
        made = cache.make_and_validate_key(key, version=version)
        store("miss" if cache._has_expired(made) else "hit", (id(cache._cache), made))

    @functools.wraps(get)
    def get_wrapper(
        self: LocMemCache,
        key: object,
        default: object = None,
        version: int | None = None,
    ) -> object:
        read(self, key, version)
        return get(self, key, default, version)

    @functools.wraps(has_key)
    def has_key_wrapper(
        self: LocMemCache, key: object, version: int | None = None
    ) -> bool:
        read(self, key, version)
        return has_key(self, key, version)

    @functools.wraps(set_)
    def set_wrapper(
        self: LocMemCache, key: str, *args: object, **kwargs: object
    ) -> None:
        set_(self, key, *args, **kwargs)
        store("set", (id(self._cache), key))

    return [
        (LocMemCache, "get", get_wrapper),
        (LocMemCache, "has_key", has_key_wrapper),
        (LocMemCache, "_set", set_wrapper),
    ]


def _translation_patches(
    touch: Callable[[str], None], store: Callable[[str, Hashable], None]
) -> list[tuple[object, str, object]]:
    """Return what to wrap so a test translating a message is credited with the
    message files its catalog came from, which Django and gettext load once."""
    init, ngettext = DjangoTranslation.__init__, DjangoTranslation.ngettext
    gettext, translation = (
        gettext_module.GNUTranslations.gettext,
        gettext_module.translation,
    )

    @functools.wraps(init)
    def init_wrapper(self: DjangoTranslation, *args: object, **kwargs: object) -> None:
        store("miss", (DjangoTranslation, identity(self)))
        try:
            init(self, *args, **kwargs)
        finally:
            store("set", (DjangoTranslation, identity(self)))

    @functools.wraps(gettext)
    def gettext_wrapper(self: gettext_module.GNUTranslations, message: str) -> str:
        store("hit", (DjangoTranslation, identity(self)))
        return gettext(self, message)

    @functools.wraps(ngettext)
    def ngettext_wrapper(
        self: DjangoTranslation, msgid1: str, msgid2: str, n: int
    ) -> str:
        store("hit", (DjangoTranslation, identity(self)))
        return ngettext(self, msgid1, msgid2, n)

    # Gettext keeps each file's messages, so a later catalog reads none.
    @functools.wraps(translation)
    def translation_wrapper(
        domain: str,
        localedir: str | None = None,
        languages: list[str] | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> gettext_module.NullTranslations:
        for path in gettext_module.find(domain, localedir, languages, all=True):
            touch(path)
        return translation(domain, localedir, languages, *args, **kwargs)

    return [
        (DjangoTranslation, "__init__", init_wrapper),
        (DjangoTranslation, "ngettext", ngettext_wrapper),
        (gettext_module.GNUTranslations, "gettext", gettext_wrapper),
        (gettext_module, "translation", translation_wrapper),
    ]


def _model_state_patch(
    use_module: Callable[[str, str], None],
) -> tuple[object, str, object]:
    """Return what to wrap to credit each model whose state the migration framework
    reads, as makemigrations does for every model, deconstructing its fields
    without running project code."""
    from_model = cast("classmethod[Any, Any, Any]", vars(ModelState)["from_model"])
    function = from_model.__func__

    @functools.wraps(function)
    def wrapper(cls: type, model: type, *args: object, **kwargs: object) -> object:
        file = getattr(sys.modules.get(model.__module__), "__file__", None) or ""
        use_module(file, f"{STATE}{model.__qualname__}")
        return function(cls, model, *args, **kwargs)

    return ModelState, "from_model", classmethod(wrapper)


def _sources_wrap(
    get_template: Callable[..., Any], touch: Callable[[str], None]
) -> Callable[..., Any]:
    """Wrap the cached template loader to credit every file a template could come
    from, as only the first lookup tries them: a file added at one of them may
    take its place."""

    @functools.wraps(get_template)
    def wrapper(self: cached.Loader, template_name: str, skip: object = None) -> Any:  # noqa: ANN401
        for loader in self.loaders:
            for origin in loader.get_template_sources(template_name):
                touch(str(origin.name))
        return get_template(self, template_name, skip)

    return wrapper


def install(  # noqa: PLR0913
    credit: Callable[[type | None], None],
    touch: Callable[[str], None],
    use_module: Callable[[str, str], None],
    credit_call: Callable[[type, str], None],
    render_line: Callable[[str, int], None],
    *,
    store: Callable[[str, Hashable], None],
) -> Callable[[], None]:
    """Report each class a test uses, each Django method it calls on one, each
    template and template line it renders, each URL module it resolves a path or
    reverses a name through, each command module it loads, each model whose
    migration state it reads, and each value it reads from or stores in the local
    memory cache; return an undo."""
    originals = []
    for owner, name, subject in patches():
        original = owner.__dict__[name]
        setattr(owner, name, _wrap(original, subject, credit))
        originals.append((owner, name, original))
    for owner, name in overridable():
        original = vars(owner)[name]
        setattr(owner, name, _call_wrap(original, name, credit_call))
        originals.append((owner, name, original))
    load = management.load_command_class

    @functools.wraps(load)
    def load_wrapper(app_name: str, name: str) -> object:
        command = load(app_name, name)
        use_module(sys.modules[type(command).__module__].__file__ or "", "")
        return command

    for owner, name, wrapper in [
        _model_state_patch(use_module),
        *_store_patches(store),
        *_translation_patches(touch, store),
        *_url_patches(use_module),
        *_settings_patches(use_module),
        (management, "load_command_class", load_wrapper),
        (
            cached.Loader,
            "get_template",
            _sources_wrap(cached.Loader.get_template, touch),
        ),
        # Text nodes render their own way, without the base class's annotation.
        *(
            (node, "render_annotated", _render_wrap(node.render_annotated, render_line))
            for node in (Node, TextNode)
        ),
    ]:
        originals.append((owner, name, vars(owner)[name]))
        setattr(owner, name, wrapper)

    def on_init(sender: type, **kwargs: object) -> None:
        credit(sender)

    def on_render(sender: object, template: Any, **kwargs: object) -> None:  # noqa: ANN401
        touch(template.origin.name)

    signals.pre_init.connect(on_init, weak=False, dispatch_uid=__name__)
    # The cached template loader reads each file once, so reads alone miss templates.
    template_rendered.connect(on_render, weak=False, dispatch_uid=__name__)

    def undo() -> None:
        # Backwards, as a method may be wrapped twice.
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)
        signals.pre_init.disconnect(dispatch_uid=__name__)
        template_rendered.disconnect(dispatch_uid=__name__)

    return undo
