"""Record which project functions and files each test uses."""

import ast
import atexit
import contextlib
import dis
import enum
import functools
import hashlib
import importlib
import importlib.util
import inspect
import os
import posixpath
import site
import stat
import sys
import sysconfig
import types
import weakref
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Hashable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import CodeType
from types import FrameType
from typing import Any
from typing import cast

from pytest_blast_radius import names
from pytest_blast_radius import templates

monitoring = sys.monitoring
events = monitoring.events
TOOL_IDS = (3, 4, 5, 2)
CACHE_TYPE = type(functools.cache(lambda: None))
# Keys for the fixtures a test requested, as "fixture:name" in its file.
FIXTURE = "fixture:"
# The key for a Python file a test read as data rather than imported.
SOURCE = "source:"
# The detail of a model's key for a test that read its migration state, which
# deconstructs its fields.
STATE = "state:"
STDLIB = (sysconfig.get_paths()["stdlib"], "<")
# An object's identity, unique among live objects as id() is, but without the audit
# event each id() call raises, which the recorder's own hook would see.
identity = object.__hash__
# The import system's own code, which import_module() runs first.
IMPORTS = ("<frozen importlib", f"{Path(importlib.__file__ or '').parent}{os.sep}")
# Packages that run tests, this one included, whose file reads and checks are
# their own business.
TOOLS = frozenset(
    {
        "_pytest",
        "coverage",
        "execnet",
        "pluggy",
        "pytest",
        "pytest_blast_radius",
        "pytest_cov",
        "xdist",
    }
)

type FunctionKey = tuple[str, str, int]
type Key = FunctionKey | str
type Lines = frozenset[int]
# The attribute names a class inherits from outside the project, its project
# ancestors' files and names, whether it is plain, and which inherited methods
# record their calls, as in Recorder.class_info.
type ClassInfo = tuple[Collection[str], list[list[str]], bool, Collection[str]]

_audit_installed = False


class Recorder:
    """Collect the functions and files each test touches through sys.monitoring.

    Functions entered while a project module runs its top level are also credited to
    that module, and functions entered outside any test or import to the session.
    """

    def __init__(self, root: Path) -> None:
        """Watch code and files under root, other than installed packages."""
        self.prefixes = tuple({f"{root}{os.sep}", f"{os.path.realpath(root)}{os.sep}"})
        self.roots = {root, Path(os.path.realpath(root))}
        # Whether file checks are the recorder's own, and not to be recorded.
        self.quiet = False
        excluded = {
            sys.prefix,
            sys.base_prefix,
            sys.exec_prefix,
            *site.getsitepackages(),
            site.getusersitepackages(),
            str(root / ".git"),
        }
        self.excluded = tuple(
            {f"{path}{os.sep}" for path in excluded}
            | {f"{os.path.realpath(path)}{os.sep}" for path in excluded}
        )
        self.paths: dict[str, str | None] = {}
        # Keyed by id: equal code objects in different files compare equal. Holding
        # the code object keeps its id from being reused.
        self.keys: dict[int, tuple[CodeType, FunctionKey, str | None]] = {}
        self.local_codes: list[CodeType] = []
        # Code whose line events fired since the sinks last changed, and so are off.
        self.disabled: dict[int, CodeType] = {}
        self.session: set[Key] = set()
        self.imports: dict[str, set[Key]] = {}
        self.loaders: dict[str, set[str]] = {}
        self.loads: dict[str, set[str]] = {}
        self.tests: dict[str, tuple[str, set[Key], dict[FunctionKey, Lines]]] = {}
        # The lines the current test ran in each function, or None outside tests.
        self.lines: dict[FunctionKey, set[int]] | None = None
        self.tested_lines: dict[FunctionKey, Lines] = {}
        # Many tests run the same lines of a function, so they share one set.
        self.line_sets: dict[Lines, Lines] = {}
        self.base = self.session
        self.fixtures: list[set[Key]] = []
        self.modules: list[tuple[CodeType, set[Key]]] = []
        # What collecting each test module, after importing it, runs.
        self.collecting: list[set[Key]] = []
        self.sinks: list[set[Key]] = [self.session]
        self.finished_fixtures: dict[object, set[Key]] = {}
        # Each project functools cache, the keys its misses ran, and the code it
        # calls; the hits each had when the current test started; and the caches
        # whose function is running.
        self.cached: list[tuple[Any, set[Key], CodeType]] = []
        self.cache_keys: dict[int, set[Key]] = {}
        self.counts: dict[int, tuple[int, int]] = {}
        self.cache_stack: list[tuple[CodeType | None, set[Key]]] = []
        self.cache_watched: set[int] = set()
        # What ran computing values a store such as Django's cache holds, by the
        # store and key, and what runs for values a read missed, until stored.
        self.stored_values: dict[Hashable, set[Key]] = {}
        self.storing: dict[Hashable, set[Key]] = {}
        # What computed the stored values the running test read, and the values it
        # stored.
        self.hits: set[Key] = set()
        self.stored_now: set[Hashable] = set()
        # Whether new caches need their functions' start events back on.
        self.restart = False
        # What the last test ran, which may have filled a cache found after it.
        self.last_test: set[Key] = set()
        self.module_count = 0
        self.tool_id = 0
        self.class_keys: dict[int, tuple[weakref.ref[type], list[FunctionKey]]] = {}
        self.covered: tuple[type, ...] = ()
        self.undo_django: Callable[[], None] = lambda: None
        self.template_libraries: Callable[
            [], tuple[dict[str, list[str]], list[str]]
        ] = lambda: ({}, [])
        self.settings_file: Callable[[], str | None] = lambda: None
        self.test_path = ""

    def start(self) -> None:
        """Start recording."""
        global _recorder, _audit_installed  # noqa: PLW0603
        self.tool_id = next(
            (tool for tool in TOOL_IDS if monitoring.get_tool(tool) is None), -1
        )
        if self.tool_id < 0:
            msg = "pytest-blast-radius found no free sys.monitoring tool id"
            raise RuntimeError(msg)
        monitoring.use_tool_id(self.tool_id, "pytest-blast-radius")
        monitoring.register_callback(self.tool_id, events.PY_START, _on_start)
        monitoring.register_callback(self.tool_id, events.PY_RETURN, _on_return)
        monitoring.register_callback(self.tool_id, events.LINE, _on_line)
        monitoring.set_events(self.tool_id, events.PY_START)
        if not _audit_installed:
            sys.addaudithook(_on_audit)
            # Checking that a file exists raises no audit event.
            os.stat = _checks(os.stat)  # ty: ignore[invalid-assignment]
            os.lstat = _checks(os.lstat)  # ty: ignore[invalid-assignment]
            os.access = _checks(os.access)  # ty: ignore[invalid-assignment]
            # Nor does finding a module through the import system's cached listing.
            importlib.util.find_spec = _probes(importlib.util.find_spec)  # ty: ignore[invalid-assignment]
            _audit_installed = True
        _recorder = self
        # pytest never unconfigures when a conftest fails to import.
        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop recording and release the tool id."""
        global _recorder  # noqa: PLW0603
        atexit.unregister(self.stop)
        _recorder = None
        monitoring.set_events(self.tool_id, 0)
        for code in self.local_codes:
            monitoring.set_local_events(self.tool_id, code, 0)
        monitoring.register_callback(self.tool_id, events.PY_START, None)
        monitoring.register_callback(self.tool_id, events.PY_RETURN, None)
        monitoring.register_callback(self.tool_id, events.LINE, None)
        monitoring.free_tool_id(self.tool_id)
        self.undo_django()

    def connect_django(self) -> None:
        """Credit tests with the Django classes and templates they use."""
        if "django" not in sys.modules:
            return
        # Not `from ... import`: that would reuse a module bound to an unloaded Django.
        django_hooks = importlib.import_module("pytest_blast_radius.django_hooks")
        self.undo_django = django_hooks.install(
            self.credit_class,
            self.touch,
            self.use_module,
            self.credit_call,
            self.render_line,
            store=self.store_event,
        )
        self.covered = django_hooks.covered()
        self.template_libraries = django_hooks.template_libraries
        self.settings_file = django_hooks.settings_file

    def project_classes(self, cls: object) -> list[FunctionKey]:
        """Return the keys of a class and the project classes it inherits from."""
        # Keyed by id: a class may define its own equality. A weak reference tells
        # whether the id still belongs to it, without keeping the class alive.
        if not issubclass(type(cls), type):
            return []
        entry = self.class_keys.get(identity(cls))
        if entry is None or entry[0]() is not cls:
            keys = [
                key
                for base in type.__getattribute__(cls, "__mro__")
                if (key := self.class_key(base)) is not None
            ]
            entry = self.class_keys[identity(cls)] = (
                weakref.ref(cast("type", cls)),
                keys,
            )
        return entry[1]

    def credit_class(self, cls: object) -> None:
        """Credit a class and the project classes it inherits from."""
        for sink in self.sinks:
            sink.update(self.project_classes(cls))

    def credit_call(self, cls: type, name: str) -> None:
        """Credit a call of a Django method on an instance of a class, as keys like
        `call:Product.save` for the class and its project ancestors."""
        keys = [
            (path, f"call:{qualname}.{name}", 0)
            for path, qualname, _ in self.project_classes(cls)
        ]
        for sink in self.sinks:
            sink.update(keys)

    def class_key(self, cls: type) -> FunctionKey | None:
        """Return a project class's key, with line 0 to tell it from functions."""
        filename = getattr(sys.modules.get(cls.__module__), "__file__", None)
        path = self.relative(filename) if isinstance(filename, str) else None
        return None if path is None else (path, cls.__qualname__, 0)

    def relative(self, filename: str) -> str | None:
        """Return a project file's path relative to the root, or None."""
        try:
            return self.paths[filename]
        except KeyError:
            pass
        result = None
        # Code compiled from strings has names such as "<string>", not paths.
        if (
            Path(filename).is_absolute()
            and not filename.startswith(self.excluded)
            and "site-packages" not in filename
        ):
            # A symlink's target is what git sees change.
            for name in (_realpath(filename), filename):
                for prefix in self.prefixes:
                    if result is None and name.startswith(prefix):
                        result = name[len(prefix) :].replace(os.sep, "/")
        self.paths[filename] = result
        return result

    def touch(
        self,
        filename: str,
        *,
        directory: bool = False,
        checked: bool = False,
        probed: bool = False,
    ) -> None:
        """Record that a file was read or rendered, checked for, or a directory
        listed, which is recorded with a trailing slash. Python files read as
        data, not imported, get a key of their own, which any change to them
        reaches; one checked for only needs to exist. A package `probed` for a
        module counts only when the project probes, as libraries such as Django do
        for every app."""
        if filename.endswith(".pyc") or "__pycache__" in filename:
            return
        # Building a Path for each of the many files libraries check costs more.
        absolute = (
            filename
            if os.path.isabs(filename)  # noqa: PTH117
            else str(Path(filename).absolute())
        )
        path = self.relative(absolute)
        if directory and path is None and Path(absolute) in self.roots:
            path = ""
        # Most files are outside the project, so this goes before walking the stack.
        if path is None:
            return
        if directory:
            path += "/"
        source = filename.endswith(".py") and not checked
        if (source or checked or directory) and self.incidental(
            libraries=source or probed
        ):
            return
        # Outside tests, tools such as coverage read their own settings, which change
        # what they report but not what tests do. Only project code's reads count.
        outside = len(self.sinks) == 1 and self.sinks[0] is self.session
        if outside and not self.from_project():
            return
        paths = {path}
        # A symlink in the project counts too: pointing it elsewhere changes it.
        for prefix in self.prefixes:
            if (name := str(absolute)).startswith(prefix):
                link = name[len(prefix) :].replace(os.sep, "/")
                paths.add(f"{link}/" if directory else link)
        for each in paths:
            key: Key = (each, SOURCE, 0) if source else each
            for sink in self.sinks:
                sink.add(key)

    def incidental(self, *, libraries: bool) -> bool:
        """Return whether a file is read or checked for its code, not its text:
        by the import system, which compiles it, unless the project probes with
        importlib.util.find_spec(); by linecache for a traceback, unless inspect
        asks, as getsource() does; or else, with `libraries`, by a library, as
        Django's error pages show source lines, so the nearest caller outside the
        standard library and this module isn't the project's."""
        frame = sys._getframe(1)
        importing = probing = lines = inspecting = False
        while frame is not None:
            name = frame.f_code.co_filename
            importing = importing or name.startswith("<frozen importlib")
            probing = probing or name.endswith(f"{os.sep}importlib{os.sep}util.py")
            lines = lines or name.endswith(f"{os.sep}linecache.py")
            inspecting = inspecting or name.endswith(f"{os.sep}inspect.py")
            if name != __file__ and not name.startswith(STDLIB):
                if (importing and not probing) or (lines and not inspecting):
                    return True
                # Test tools look at files for their own sake.
                package = str(frame.f_globals.get("__name__", "")).partition(".")[0]
                if package in TOOLS:
                    return True
                return (libraries or importing) and self.relative(name) is None
            frame = frame.f_back
        return libraries or lines or importing

    def use_module(self, filename: str, detail: str) -> None:
        """Record that a framework used a module, such as a URLconf, for a test.

        Tests are credited with a key for the module and what the framework used
        it for, such as "path:/items/", with line 0, which a colon tells from a
        class's key. A change to the module's top
        level selects them, as it does its functions, and selection may narrow
        that by the detail.
        """
        path = self.relative(filename)
        if path is not None:
            for sink in self.sinks:
                sink.add((path, detail, 0))

    def render_line(self, filename: str, line: int) -> None:
        """Record that a test rendered a template's node starting at a line.

        Tests are credited with a key for the template, named "template:" with
        line 1, and its rendered lines, as with a function.
        """
        path = self.relative(filename)
        if path is None:
            return
        key = (path, templates.TEMPLATE, 1)
        for sink in self.sinks:
            sink.add(key)
        if self.lines is not None:
            self.lines.setdefault(key, set()).add(line)

    def from_project(self) -> bool:
        """Return whether project code is running, outside this module."""
        frame = sys._getframe(2)
        while frame is not None:
            if self.relative(frame.f_code.co_filename) is not None:
                return True
            frame = frame.f_back
        return False

    def enter_module(self, code: CodeType, path: str) -> None:  # pragma: no cover
        """Credit a module's import to the importer and watch for its end."""
        for sink in self.sinks:
            if sink is not self.session:
                sink.add(path)
        self.modules.append((code, self.imports.setdefault(path, set())))
        self.local_codes.append(code)
        monitoring.set_local_events(self.tool_id, code, events.PY_RETURN)
        self.update_sinks()

    def watch_lines(self, code: CodeType) -> None:  # pragma: no cover
        """Record a function through its line events, which can be turned back on."""
        self.local_codes.append(code)
        monitoring.set_local_events(self.tool_id, code, events.LINE)

    def leave_module(self, code: CodeType) -> None:  # pragma: no cover
        """Stop crediting a module, and any inner imports that raised."""
        for index in range(len(self.modules) - 1, -1, -1):
            if self.modules[index][0] is code:
                del self.modules[index:]
                break
        self.update_sinks()

    def start_collecting(self, path: str) -> None:
        """Credit what collecting a test module runs, as pytest reading attributes
        of its objects, to the module, as importing it would be."""
        self.collecting.append(self.imports.setdefault(path, set()))
        self.update_sinks()

    def stop_collecting(self) -> None:
        """Stop crediting the test module collected last."""
        self.collecting.pop()
        self.update_sinks()

    def prune_modules(self) -> None:
        """Stop crediting modules whose import ended by raising, as a module-level
        skip does, which no return event reports."""
        running = set()
        frame = sys._getframe()
        while frame is not None:
            running.add(id(frame.f_code))
            frame = frame.f_back
        kept = [entry for entry in self.modules if id(entry[0]) in running]
        if len(kept) != len(self.modules):
            self.modules[:] = kept
            self.update_sinks()

    def update_sinks(self) -> None:
        """Choose the sets the next function is credited to.

        Line events stay off once they fire, until the sets change, so each line
        costs one event for each set rather than one for each time it runs.
        """
        sinks = (
            []
            if self.base is self.session and (self.modules or self.collecting)
            else [self.base]
        )
        sinks += self.collecting
        sinks += self.fixtures
        sinks += (keys for _, keys in self.modules)
        sinks += (keys for _, keys in self.cache_stack)
        self.sinks = sinks
        # Popping one at a time keeps up with other threads adding to it.
        while self.disabled:
            _, code = self.disabled.popitem()
            monitoring.set_local_events(self.tool_id, code, 0)
            # Cached functions also report returning.
            cached = events.PY_RETURN if identity(code) in self.cache_keys else 0
            monitoring.set_local_events(self.tool_id, code, events.LINE | cached)

    def start_test(self, nodeid: str, path: str) -> None:
        """Start crediting a test."""
        self.watch_caches()
        self.cache_stack.clear()
        self.storing.clear()
        self.stored_now.clear()
        keys: set[Key] = set()
        self.tested_lines = {}
        self.tests[nodeid] = (path, keys, self.tested_lines)
        self.test_path = path
        self.base = keys
        self.lines = {}
        self.modules.clear()
        self.fixtures.clear()
        self.update_sinks()

    def credit_definition(self, code: CodeType) -> None:
        """Credit the running test with its own function and module, which decide
        whether it runs even when a mark skips it before its code does."""
        path = self.relative(code.co_filename)
        if path is not None:
            self.base |= {(path, code.co_qualname, code.co_firstlineno), path}

    def finish_test(self, fixturenames: Iterable[str]) -> None:
        """Add the wider-scoped fixtures the test used and stop crediting it."""
        names = set(fixturenames)
        # What wider-scoped fixtures and cache hits ran counts as run in full, over
        # the lines the test ran in the same functions itself.
        shared, self.hits = self.hits, set()
        for fixturedef, keys in self.finished_fixtures.items():
            if getattr(fixturedef, "argname", None) in names:
                shared |= keys
        # A cache cleared during the test may have been hit before.
        counts = self.cache_counts()
        for cache, keys, _ in self.cached:
            now, before = counts[id(cache)], self.counts.get(id(cache), (0, 0))
            if now[0] > before[0] or now < before:
                shared |= keys
        self.counts = counts
        self.base |= shared
        for key in shared:
            if self.lines is not None and isinstance(key, tuple):
                self.lines.pop(key, None)
        # Tests request fixtures by name, so a fixture defined for that name later
        # changes what they get.
        self.base |= {(self.test_path, f"{FIXTURE}{name}", 0) for name in names}
        self.last_test = self.base
        self.cache_stack.clear()
        for key, ran in (self.lines or {}).items():
            line_set = frozenset(ran)
            self.tested_lines[key] = self.line_sets.setdefault(line_set, line_set)
        self.lines = None
        self.base = self.session
        self.modules.clear()
        self.fixtures.clear()
        self.update_sinks()

    def start_fixture(self) -> set[Key]:
        """Start crediting a wider-scoped fixture's setup."""
        keys: set[Key] = set()
        self.fixtures.append(keys)
        self.update_sinks()
        return keys

    def finish_fixture(self, fixturedef: object, keys: set[Key]) -> None:
        """Keep a fixture's keys to credit every test that uses it."""
        if self.fixtures and self.fixtures[-1] is keys:
            self.fixtures.pop()
        self.finished_fixtures[fixturedef] = keys
        self.update_sinks()

    def drop_fixture(self, fixturedef: object) -> None:
        """Forget a fixture once it is torn down."""
        self.finished_fixtures.pop(fixturedef, None)

    def watch_caches(self) -> None:
        """Find project functools caches, to credit a test that hits one with what
        computing its values ran, as no test would rerun it.

        A cache found after it was used gets what the last test and the session
        ran, which computed its values; one used between tests, as while
        collecting, credits the session.
        """
        if len(sys.modules) != self.module_count:
            self.module_count = len(sys.modules)
            self.find_caches(self.namespaces(), self.last_test | self.session)
        # A cached function that already ran has its start events off.
        if self.restart:
            self.restart = False
            monitoring.restart_events()
        counts = self.cache_counts()
        for cache, keys, _ in self.cached:
            if counts[id(cache)] != self.counts.get(id(cache)):
                self.session |= keys
        self.counts = counts

    def find_caches(self, namespaces: list[dict[str, Any]], filled: set[Key]) -> None:
        """Start watching caches in these namespaces.

        One already used gets `filled`, what may have computed its values, and
        one hit already credits the session.
        """
        for cache in (
            value for space in namespaces for value in cached_functions(space)
        ):
            code = _cached_code(cache)
            if code is None or id(cache) in self.counts:
                continue
            keys = self.cache_keys.setdefault(identity(code), set())
            info = cache.cache_info()
            if info.hits or info.misses:
                keys |= filled
            if info.hits:
                self.session |= keys
            self.cached.append((cache, keys, code))
            self.counts[id(cache)] = (info.hits, info.misses)
            self.restart = True

    def module_loaded(
        self, code: CodeType, module_dict: dict[str, Any], frame: FrameType | None
    ) -> None:
        """Watch the caches a project module defines as soon as it loads, crediting
        any it used while loading with what loading it ran, and note the project
        file that loaded it, from its caller's frame, as `import_module()` loads
        modules no import statement names."""
        path = self.relative(code.co_filename)
        name = module_dict.get("__name__")
        if path is not None and isinstance(name, str):
            self.find_caches(
                module_namespaces(name, module_dict), self.imports.get(path, set())
            )
            # The first caller outside the import system, unless a library, as
            # Django loading a URLconf, which is a framework's doing. Only a
            # function loading it during a test counts: an import statement is
            # already known, and what loads outside tests may affect any test.
            while frame is not None and frame.f_code.co_filename.startswith(IMPORTS):
                frame = frame.f_back
            loader = self.relative(frame.f_code.co_filename) if frame else None
            # A module whose top level loads it through project functions, as
            # oscar's get_class() does, imports it as a statement would.
            caller = frame
            while (
                caller is not None
                and caller.f_code.co_name != "<module>"
                and (
                    caller.f_code.co_filename.startswith(IMPORTS)
                    or self.relative(caller.f_code.co_filename) is not None
                )
            ):
                caller = caller.f_back
            importer = self.relative(caller.f_code.co_filename) if caller else None
            if importer is not None and importer != path and caller is not frame:
                self.loads.setdefault(importer, set()).add(path)
            if (
                loader is not None
                and loader != path
                and frame is not None
                and frame.f_code.co_name != "<module>"
                and self.base is not self.session
            ):
                self.loaders.setdefault(path, set()).add(loader)

    def cache_counts(self) -> dict[int, tuple[int, int]]:
        """Return each cache's hits and misses."""
        return {
            id(cache): (info.hits, info.misses)
            for cache, _, _ in self.cached
            if (info := cache.cache_info())
        }

    def enter_cache(self, code: CodeType, keys: set[Key]) -> None:  # pragma: no cover
        """Credit what a cached function runs to its cache until it returns."""
        known = any(entry is keys for _, entry in self.cache_stack)
        self.cache_stack.append((code, keys))
        if id(code) not in self.cache_watched:
            self.cache_watched.add(id(code))
            self.local_codes.append(code)
            monitoring.set_local_events(
                self.tool_id, code, events.LINE | events.PY_RETURN
            )
        # A cache already on the stack already gets what runs.
        if not known:
            self.update_sinks()

    def leave_cache(self, code: CodeType) -> None:  # pragma: no cover
        """Stop crediting a cache, and any inner ones that raised."""
        for index in range(len(self.cache_stack) - 1, -1, -1):
            if self.cache_stack[index][0] is code:
                left = {id(keys) for _, keys in self.cache_stack[index:]}
                del self.cache_stack[index:]
                if left - {id(keys) for _, keys in self.cache_stack}:
                    self.update_sinks()
                break

    def store_event(self, event: str, token: Hashable) -> None:
        """Note a store's "miss", "set" or "hit" of a value."""
        {"miss": self.stored_miss, "set": self.stored, "hit": self.stored_hit}[event](
            token
        )

    def stored_miss(self, token: Hashable) -> None:
        """Start crediting what runs after a store, such as Django's cache, misses a
        value, to the value, as code computing it to store does, until it is
        stored."""
        keys: set[Key] = set()
        self.storing[token] = keys
        self.cache_stack.append((None, keys))
        self.update_sinks()

    def stored(self, token: Hashable) -> None:
        """Remember what computed a stored value: what ran since a read missed it,
        or else what the running test or the session has run, unless it already
        stored the value, as code that stores a value twice does."""
        keys = self.storing.pop(token, None)
        if keys is None and token in self.stored_now:
            return
        self.stored_now.add(token)
        self.stored_values[token] = self.base if keys is None else keys
        for index, (_, entry) in enumerate(self.cache_stack):
            if entry is keys:
                del self.cache_stack[index]
                self.update_sinks()
                break

    def stored_hit(self, token: Hashable) -> None:
        """Credit a read of a stored value with what computed it, which a test
        that only reads the value, perhaps stored by another test, doesn't run."""
        keys = self.stored_values.get(token)
        if keys is not None:
            self.hits |= keys
            for sink in self.sinks:
                if sink is not keys:
                    sink.update(keys)

    def namespaces(self) -> list[dict[str, Any]]:
        """Return the globals of project modules and the classes they define,
        including nested ones."""
        return [
            namespace
            for name, module_dict in self.module_dicts()
            for namespace in module_namespaces(name, module_dict)
        ]

    def module_dicts(self) -> list[tuple[str, dict[str, Any]]]:
        """Return the names and globals of loaded project modules."""
        result = []
        for name, module in list(sys.modules.items()):
            try:
                # Lazy modules load on attribute access, but not through object.
                module_dict = object.__getattribute__(module, "__dict__")
            except AttributeError:
                continue
            filename = module_dict.get("__file__")
            if isinstance(filename, str) and self.relative(filename) is not None:
                result.append((name, module_dict))
        return result

    def export(self) -> dict[str, Any]:
        """Stop crediting code, which other threads may still run, and return
        everything recorded in the compact stored format."""
        global _recorder  # noqa: PLW0603
        _recorder = None
        for event in (events.PY_START, events.PY_RETURN, events.LINE):
            monitoring.register_callback(self.tool_id, event, None)
        modules = {}
        covered = set()
        classes = {}
        enums: set[str] = set()
        for name, module_dict in self.module_dicts():
            path = self.relative(module_dict["__file__"])
            if path is None or not path.endswith(".py"):
                continue
            modules[name] = path
            for value in list(module_dict.values()):
                if not (
                    issubclass(type(value), type)
                    and vars(value).get("__module__") == name
                ):
                    continue
                if issubclass(value, self.covered):
                    covered.add(value.__qualname__.partition(".")[0])
                # Enums whose bases don't look like one, as `Color(Base)`.
                if issubclass(value, enum.Enum):
                    enums.add(f"{path}:{value.__qualname__}")
                if "." not in value.__qualname__:
                    classes[path, value.__qualname__] = self.class_info(value)
        data = encode(
            self.tests,
            self.imports,
            self.session,
            modules,
            # A snapshot: dis may run code for the first time, which adds keys.
            names={
                key: mentioned_names(code) for code, key, _ in list(self.keys.values())
            },
            covered=covered,
            loaded=sys.modules,
            classes=classes,
        )
        # Template tag libraries, by the names templates load them by, and the
        # ones every template loads.
        libraries, builtins = self.template_libraries()
        data["libraries"] = {
            path: names
            for module, names in libraries.items()
            if (path := self.module_path(module)) is not None
        }
        data["builtins"] = sorted(
            path for module in builtins if (path := self.module_path(module))
        )
        filename = self.settings_file()
        data["settings"] = self.relative(filename) if filename else None
        data["enums"] = sorted(enums)
        data["loaders"] = {
            path: sorted(loaders) for path, loaders in self.loaders.items()
        }
        data["loads"] = {path: sorted(loads) for path, loads in self.loads.items()}
        return data

    def module_path(self, name: str) -> str | None:
        """Return a loaded project module's path."""
        filename = getattr(sys.modules.get(name), "__file__", None)
        return self.relative(filename) if isinstance(filename, str) else None

    def class_info(self, cls: type) -> ClassInfo:
        """Return what a class inherits, its project ancestors and whether it is plain.

        What it inherits are the attribute names of its ancestors outside the
        project, whose code the recording doesn't see looking them up. Only plain
        classes keep their attributes to project code: a metaclass or an
        `__init_subclass__` may read them when the class is made, and a library base
        class may read them without the recording seeing it.
        """
        mro = type.__getattribute__(cls, "__mro__")[1:]
        ancestors = [key for base in mro if (key := self.class_key(base))]
        inherited = frozenset(
            name for base in mro if self.class_key(base) is None for name in vars(base)
        )
        return (
            inherited,
            [list(key[:2]) for key in ancestors],
            type(cls) is type
            # NamedTuple makes fields of class attributes, with a metaclass that
            # leaves no trace.
            and tuple not in mro
            and len(ancestors) == sum(base.__module__ != "builtins" for base in mro)
            and not any(
                "__init_subclass__" in vars(base)
                for base in mro
                if base.__module__ != "builtins"
            ),
            # Inherited methods whose calls credit_call records.
            frozenset(
                name
                for name in inherited
                if getattr(
                    next(vars(base)[name] for base in mro if name in vars(base)),
                    "blast_radius_calls",
                    False,
                )
            ),
        )


_recorder: Recorder | None = None


def module_namespaces(name: str, module_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a module's globals and the classes it defines, including nested
    ones."""
    result = []
    # Classes, by id: each call of vars() on one makes a new mapping.
    seen: set[int] = set()
    pending = [module_dict]
    while pending:
        namespace = pending.pop()
        result.append(namespace)
        for value in list(namespace.values()):
            if (
                issubclass(type(value), type)
                and id(value) not in seen
                and vars(value).get("__module__") == name
            ):
                seen.add(id(value))
                pending.append(vars(value))
    return result


def cached_functions(namespace: dict[str, Any]) -> list[Any]:
    """Return the functools caches in a module or class namespace, including those
    under other decorators, as properties, or static or class methods."""
    result = []
    pending = list(namespace.values())
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if type(value) is CACHE_TYPE:
            result.append(value)
        if type(value) in {staticmethod, classmethod}:
            pending.append(value.__func__)
        elif type(value) is property:
            pending += [value.fget, value.fset, value.fdel]
        elif type(value) is CACHE_TYPE or type(value) is types.FunctionType:
            # Not getattr: that could run arbitrary code.
            pending.append(
                object.__getattribute__(value, "__dict__").get("__wrapped__")
            )
            # A decorator may keep a cache in its wrapper's closure.
            for cell in getattr(value, "__closure__", None) or ():
                with contextlib.suppress(ValueError):
                    pending.append(cell.cell_contents)
    return result


def _cached_code(cache: Any) -> CodeType | None:  # noqa: ANN401
    """Return the code a cache runs on a miss, through wrappers and partials."""
    target = cache
    while True:
        try:
            target = inspect.unwrap(target)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(target, functools.partial):
            break
        target = target.func
    code = getattr(target, "__code__", None)
    return code if isinstance(code, CodeType) else None


def _on_start(code: CodeType, instruction_offset: int) -> object:  # pragma: no cover
    # Coverage can't see sys.monitoring callbacks: monitoring is off while they run.
    recorder = _recorder
    if recorder is None:
        return monitoring.DISABLE
    ident = identity(code)
    entry = recorder.keys.get(ident)
    if entry is None:
        path = recorder.relative(code.co_filename)
        # Python 3.14 evaluates annotations lazily, often outside tests: pytest reads
        # test signatures while collecting. Their lines are top-level code anyway.
        if path is None or code.co_name == "__annotate__":
            return monitoring.DISABLE
        if code.co_name == "<module>":
            recorder.enter_module(code, path)
            return None
        first = code.co_varnames[0] if code.co_argcount else None
        entry = (
            code,
            (path, code.co_qualname, code.co_firstlineno),
            first if first in {"self", "cls"} else None,
        )
        recorder.keys[ident] = entry
        recorder.watch_lines(code)
    _, key, receiver = entry
    # A cached function runs only on a miss, so every start counts.
    cache = recorder.cache_keys.get(ident)
    if cache is not None:
        recorder.enter_cache(code, cache)
    if receiver is None:
        # Line events record the function from now on, and can be turned back on.
        return monitoring.DISABLE if cache is None else None
    # A method credits the class it runs for, which may only set attributes that
    # inherited code reads, so its start event stays on.
    for sink in recorder.sinks:
        sink.add(key)
    value = sys._getframe(1).f_locals.get(receiver)
    recorder.credit_class(value if receiver == "cls" else type(value))
    return None


def _on_line(code: CodeType, line: int) -> object:  # pragma: no cover
    recorder = _recorder
    ident = identity(code)
    entry = None if recorder is None else recorder.keys.get(ident)
    if recorder is None or entry is None:
        return monitoring.DISABLE
    key = entry[1]
    for sink in recorder.sinks:
        sink.add(key)
    if recorder.lines is not None:
        recorder.lines.setdefault(key, set()).add(line)
    recorder.disabled[ident] = code
    return monitoring.DISABLE


def _on_return(
    code: CodeType, instruction_offset: int, retval: object
) -> None:  # pragma: no cover
    if _recorder is not None:
        if code.co_name == "<module>":
            frame = sys._getframe(1)
            _recorder.module_loaded(code, frame.f_globals, frame.f_back)
        _recorder.leave_module(code)
        _recorder.leave_cache(code)


_lstat = os.lstat
_readlink = os.readlink
# How many symlinks resolving a path follows, as the kernel's limit, for loops.
LINKS = 40


def _realpath(path: str) -> str:
    """Return an absolute path with symlinks resolved, as os.path.realpath does,
    but through os.lstat and os.readlink as they were when recording loaded, since
    tests may mock them, and the recording's own wrappers would record the checks."""
    if os.name != "posix":  # pragma: no cover
        return os.path.realpath(path)
    resolved = "/"
    pending = path.split("/")[::-1]
    links = 0
    while pending:
        part = pending.pop()
        if part in {"", "."}:
            continue
        if part == "..":
            resolved = posixpath.dirname(resolved)
            continue
        candidate = posixpath.join(resolved, part)
        try:
            target = (
                _readlink(candidate)
                if links < LINKS and stat.S_ISLNK(_lstat(candidate).st_mode)
                else None
            )
        except (OSError, ValueError):
            target = None
        if target is None:
            resolved = candidate
            continue
        links += 1
        if target.startswith("/"):
            resolved = "/"
        pending.extend(target.split("/")[::-1])
    return resolved


def _checks(check: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap os.stat, os.lstat or os.access to credit the file a test checks, as
    os.path.exists() does, whether or not it exists."""

    @functools.wraps(check)
    def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        recorder = _recorder
        if (
            recorder is not None
            and not recorder.quiet
            and isinstance(path, str | bytes | os.PathLike)
        ):
            recorder.quiet = True
            # A path the recording can't place, as when the working directory is
            # gone, goes unrecorded rather than changing what the check does.
            try:
                with contextlib.suppress(OSError, ValueError):
                    recorder.touch(os.fsdecode(path), checked=True)
            finally:
                recorder.quiet = False
        return check(path, *args, **kwargs)

    # Code such as shutil checks which arguments a function takes through these.
    for supported in (
        os.supports_dir_fd,
        os.supports_effective_ids,
        os.supports_fd,
        os.supports_follow_symlinks,
    ):
        if check in supported:
            supported.add(wrapper)
    return wrapper


def _probes(find_spec: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap importlib.util.find_spec to credit the package directories a probe
    for a submodule looks in, where adding the module changes the answer."""

    @functools.wraps(find_spec)
    def wrapper(name: str, package: str | None = None) -> Any:  # noqa: ANN401
        recorder = _recorder
        parent = name.rpartition(".")[0]
        if recorder is not None and not recorder.quiet and parent:
            paths = getattr(sys.modules.get(parent), "__path__", None) or ()
            recorder.quiet = True
            try:
                for path in list(paths):
                    recorder.touch(os.fspath(path), directory=True, probed=True)
            finally:
                recorder.quiet = False
        return find_spec(name, package)

    return wrapper


# Audit events that read a file, or list a directory, named by their first argument.
LISTS = frozenset({"os.listdir", "os.scandir"})
EVENTS = frozenset({"open", "sqlite3.connect", *LISTS})


def _on_audit(
    event: str, args: tuple[Any, ...], events: frozenset[str] = EVENTS
) -> None:  # pragma: no cover
    # Coverage can't see audit hooks either: CPython runs them with tracing off.
    # Python audits millions of events, as each id() call, so the check that drops
    # nearly all of them looks up no globals, which the hook also outlives at exit.
    if event not in events or _recorder is None or not args:
        return
    # Listing with no path lists the working directory.
    target = "." if args[0] is None and event in LISTS else args[0]
    if isinstance(target, str | bytes | os.PathLike):
        _recorder.touch(os.fsdecode(target), directory=event in LISTS)


class Table:
    """Intern file paths and functions into lists of unique entries."""

    def __init__(self) -> None:
        """Start with empty tables."""
        self.files: dict[str, int] = {}
        self.functions: dict[tuple[int, str, int], int] = {}

    def file(self, path: str) -> int:
        """Return a file's index."""
        return self.files.setdefault(path, len(self.files))

    def function(self, file: int, qualname: str, line: int) -> int:
        """Return a function's index."""
        return self.functions.setdefault((file, qualname, line), len(self.functions))

    def classes(
        self, classes: Mapping[tuple[str, str], ClassInfo]
    ) -> tuple[list[list[Any]], list[list[str]]]:
        """Return classes with interned files and name sets, and those name sets."""
        sets: dict[tuple[str, ...], int] = {}
        encoded = [
            [
                self.file(path),
                qualname,
                sets.setdefault(tuple(sorted(names)), len(sets)),
                [[self.file(file), name] for file, name in ancestors],
                plain,
                sets.setdefault(tuple(sorted(tracked)), len(sets)),
            ]
            for (path, qualname), (names, ancestors, plain, tracked) in classes.items()
        ]
        return encoded, [list(names) for names in sets]

    def keys(self, keys: Iterable[Key]) -> list[list[int]]:
        """Return sorted function indices and file indices."""
        functions = []
        files = []
        for key in keys:
            if isinstance(key, str):
                files.append(self.file(key))
            else:
                path, qualname, line = key
                functions.append(self.function(self.file(path), qualname, line))
        return [sorted(functions), sorted(files)]


def encode(  # noqa: PLR0913
    tests: dict[str, tuple[str, set[Key], dict[FunctionKey, Lines]]],
    imports: dict[str, set[Key]],
    session: set[Key],
    modules: dict[str, str],
    *,
    names: Mapping[FunctionKey, tuple[Collection[str], ...]],
    covered: Iterable[str],
    loaded: Iterable[str],
    classes: Mapping[tuple[str, str], ClassInfo],
) -> dict[str, Any]:
    """Return recorded keys in the stored format.

    Each test lists the lines it ran in functions as pairs of a function and an
    entry in `line_sets`, which tests share. `names` are the global names,
    attribute names and strings each function's code mentions, `covered` the
    project classes the Django hooks credit, and `loaded` every module imported.
    `classes` map each top-level project class to the attribute names it inherits
    from outside the project, an entry in `inherited`, its project ancestors and
    whether it is plain.
    """
    table = Table()
    line_sets: dict[Lines, int] = {}

    def encode_lines(lines: dict[FunctionKey, Lines]) -> list[list[int]]:
        return sorted(
            [
                table.functions[table.file(path), qualname, line],
                line_sets.setdefault(ran, len(line_sets)),
            ]
            for (path, qualname, line), ran in lines.items()
        )

    data = {
        "tests": {
            nodeid: [table.file(path), *table.keys(keys), encode_lines(lines)]
            for nodeid, (path, keys, lines) in tests.items()
        },
        "imports": [
            [table.file(path), *table.keys(keys)] for path, keys in imports.items()
        ],
        "session": table.keys(session),
        "modules": {name: table.file(path) for name, path in modules.items()},
    }
    data["names"] = []
    for (path, qualname, line), mentioned in names.items():
        index = table.functions.get((table.files.get(path, -1), qualname, line))
        if index is not None and any(mentioned):
            data["names"].append([index, *map(sorted, mentioned)])
    data["classes"], data["inherited"] = table.classes(classes)
    data["covered"] = sorted(covered)
    data["loaded"] = sorted(loaded)
    data["line_sets"] = [sorted(lines) for lines in line_sets]
    data["files"] = list(table.files)
    data["functions"] = [list(function) for function in table.functions]
    return data


def carry(
    data: dict[str, Any],
    affected: set[str],
    moves: dict[str, Callable[[int], int | None]],
) -> dict[str, Any]:
    """Return a recording without affected tests, with changed files' lines moved.

    The tests a change does not affect ran none of the changed lines, and run the
    same code as before, maybe at new lines. `moves` maps a changed file's old lines
    to new ones. Dropping affected tests makes any that did not run unknown, so they
    run next time.
    """
    files = data["files"]
    function_moves = [moves.get(files[file]) for file, _, _ in data["functions"]]
    line_sets = list(data["line_sets"])
    moved: dict[tuple[int, int], int] = {}

    def move(function: int, line_set: int) -> list[int]:
        move_line = function_moves[function]
        if move_line is None:
            return [function, line_set]
        if (function, line_set) not in moved:
            moved[function, line_set] = len(line_sets)
            line_sets.append(
                sorted(
                    new
                    for line in data["line_sets"][line_set]
                    if (new := move_line(line)) is not None
                )
            )
        return [function, moved[function, line_set]]

    return {
        **data,
        "functions": [
            [file, qualname, (move_line(line) or line) if move_line else line]
            for (file, qualname, line), move_line in zip(
                data["functions"], function_moves, strict=True
            )
        ],
        "line_sets": line_sets,
        "tests": {
            nodeid: [*test[:3], [move(*pair) for pair in test[3]]]
            for nodeid, test in data["tests"].items()
            if nodeid not in affected
        },
    }


def _merged(parts: Sequence[dict[str, Any]], key: str) -> dict[str, list[str]]:
    """Return the union of each part's lists of paths under a key."""
    merged: dict[str, set[str]] = {}
    for part in parts:
        for path, paths in part.get(key, {}).items():
            merged.setdefault(path, set()).update(paths)
    return {path: sorted(paths) for path, paths in merged.items()}


def merge(parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Combine recordings from several processes or runs; later tests win.

    Only the files, functions and line sets that the kept entries use carry over.
    """
    table = Table()
    line_sets: dict[tuple[int, ...], int] = {}
    tests: dict[str, list[Any]] = {}
    imports: dict[int, tuple[set[int], set[int]]] = {}
    session: tuple[set[int], set[int]] = (set(), set())
    modules: dict[str, int] = {}
    for part in reversed(parts):
        kept = [
            (nodeid, test)
            for nodeid, test in part["tests"].items()
            if nodeid not in tests
        ]
        groups = [
            *((test[1], test[2]) for _, test in kept),
            *((functions, files) for _, functions, files in part["imports"]),
            part["session"],
        ]
        used_sets = {index for _, test in kept for _, index in test[3]}
        used_files = set(part["modules"].values()).union(
            (test[0] for _, test in kept),
            (entry[0] for entry in part["imports"]),
            *(files for _, files in groups),
        )
        file_map = {index: table.file(part["files"][index]) for index in used_files}
        function_map = {}
        for index in set().union(
            *(functions for functions, _ in groups),
            (function for _, test in kept for function, _ in test[3]),
        ):
            file, qualname, line = part["functions"][index]
            function_map[index] = table.function(
                table.file(part["files"][file]), qualname, line
            )
        set_map = {
            index: line_sets.setdefault(tuple(part["line_sets"][index]), len(line_sets))
            for index in used_sets
        }
        for nodeid, (path, functions, files, lines) in kept:
            tests[nodeid] = [
                file_map[path],
                sorted(map(function_map.__getitem__, functions)),
                sorted(map(file_map.__getitem__, files)),
                sorted(
                    [function_map[function], set_map[index]]
                    for function, index in lines
                ),
            ]
        for path, functions, files in part["imports"]:
            entry = imports.setdefault(file_map[path], (set(), set()))
            entry[0].update(map(function_map.__getitem__, functions))
            entry[1].update(map(file_map.__getitem__, files))
        session[0].update(map(function_map.__getitem__, part["session"][0]))
        session[1].update(map(file_map.__getitem__, part["session"][1]))
        for name, path in part["modules"].items():
            modules.setdefault(name, file_map[path])
    names: dict[int, tuple[set[str], ...]] = {}
    classes: dict[tuple[str, str], ClassInfo] = {}
    for part in parts:
        files = part["files"]
        for index, *mentioned in part["names"]:
            file, qualname, line = part["functions"][index]
            function = table.functions.get(
                (table.files.get(files[file], -1), qualname, line)
            )
            if function is not None:
                known = names.setdefault(function, tuple(set() for _ in mentioned))
                for kept, new in zip(known, mentioned, strict=True):
                    kept.update(new)
        for file, qualname, inherited, ancestors, plain, tracked in part["classes"]:
            classes[files[file], qualname] = (
                part["inherited"][inherited],
                [[files[ancestor], name] for ancestor, name in ancestors],
                plain,
                part["inherited"][tracked],
            )
    paths = list(table.files)
    loaded = {paths[index] for index in modules.values()}
    # Classes in modules no longer loaded may be gone.
    encoded_classes, inherited = table.classes(
        {key: info for key, info in classes.items() if key[0] in loaded}
    )
    return {
        "tests": tests,
        "imports": [
            [path, sorted(functions), sorted(files)]
            for path, (functions, files) in imports.items()
        ],
        "session": [sorted(session[0]), sorted(session[1])],
        "modules": modules,
        "names": [
            [index, *map(sorted, mentioned)] for index, mentioned in names.items()
        ],
        "classes": encoded_classes,
        "inherited": inherited,
        "covered": sorted({name for part in parts for name in part["covered"]}),
        "loaded": sorted({name for part in parts for name in part["loaded"]}),
        # The tests each test file collected; later collections win.
        "collected": {
            path: nodeids
            for part in parts
            for path, nodeids in part.get("collected", {}).items()
        },
        "libraries": {
            path: names for part in parts for path, names in part["libraries"].items()
        },
        "builtins": sorted({path for part in parts for path in part["builtins"]}),
        "enums": sorted({name for part in parts for name in part.get("enums", [])}),
        # The project files that loaded each module, as through import_module().
        "loaders": _merged(parts, "loaders"),
        # The modules each module's top level loaded through project functions.
        "loads": _merged(parts, "loads"),
        "settings": next(
            (part["settings"] for part in reversed(parts) if part["settings"]), None
        ),
        "line_sets": [list(lines) for lines in line_sets],
        "files": list(table.files),
        "functions": [list(function) for function in table.functions],
    }


@dataclass
class Analysis:
    """What loaded project modules import, define and refer to at the top level."""

    edges: dict[str, list[str]]
    references: dict[str, list[list[Any]]]
    stars: dict[str, list[str]]
    defined: set[str]
    parsed: dict[str, Any]
    # Project modules each one imports that didn't load, as an optional one.
    missing: dict[str, list[str]]
    # Project modules whose objects each one's imports bind, as `from pkg import a`.
    holds: dict[str, list[str]]


def parse_module(source: bytes, package: str) -> list[Any]:
    """Return a module's top-level statements and every module it imports.

    Each statement is its kind and the names it defines, refers to plainly and as
    attributes, imports and derives from.
    """
    tree = ast.parse(source)
    imported = {
        module
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for module in names.imported_modules(node, package)
    }
    statements = [
        [
            statement.kind,
            *map(
                sorted,
                (
                    statement.defined,
                    statement.names,
                    statement.attributes,
                    statement.modules,
                    statement.derives,
                ),
            ),
        ]
        for statement in names.module_statements(tree, package)
    ]
    return [statements, sorted(imported)]


def analyse_modules(
    root: Path, modules: dict[str, str], parsed: Mapping[str, Any]
) -> Analysis:
    """Return what each loaded project module imports and refers to.

    `edges` are the project modules each one imports anywhere. `references` map
    each top-level name defined in the project to the top-level statements that
    refer to it, as a plain name, an attribute or string, or an import. `stars`
    are the modules each one imports everything from. `parsed` holds earlier
    results of parse_module by content and package, and the returned `parsed`
    holds this run's.
    """
    edges = {}
    statements = {}
    used = {}
    missing = {}
    holds = {}
    roots = {name.partition(".")[0] for name in modules}
    for name, path in modules.items():
        package = name if path.endswith("__init__.py") else name.rpartition(".")[0]
        try:
            source = (root / path).read_bytes()
            key = f"{hashlib.sha256(source).hexdigest()} {package}"
            used[key] = parsed.get(key) or parse_module(source, package)
        except (OSError, SyntaxError, ValueError):
            continue
        statements[path], imported = used[key]
        targets = set()
        for module in imported:
            parts = module.split(".")
            for end in range(1, len(parts) + 1):
                target = modules.get(".".join(parts[:end]))
                if target is not None and target != path:
                    targets.add(target)
        edges[path] = sorted(targets)
        # `from pkg.a import b` binds module b, if it is one, but not module pkg.a;
        # `import pkg.a` binds package pkg, which holds pkg.a.
        if held := {
            modules[module]
            for kind, binds, _, _, imports, _ in statements[path]
            if kind == "import"
            for module in [*imports, *binds]
            if module in modules
            and (
                module in binds
                or not any(other.startswith(f"{module}.") for other in imports)
            )
        } - {path}:
            holds[path] = sorted(held)
        # Only a package's names can be modules that didn't load.
        if absent := sorted(
            module
            for module in imported
            if module not in modules
            and module.partition(".")[0] in roots
            and modules.get(module.rpartition(".")[0], "__init__.py").endswith(
                "__init__.py"
            )
        ):
            missing[path] = absent
    defined = {
        name
        for module_statements in statements.values()
        for statement in module_statements
        for name in statement[1]
    }
    references: dict[str, list[list[Any]]] = {}
    stars: dict[str, list[str]] = {}
    for path, module_statements in statements.items():
        for kind, binds, plain, attributes, imports, derives in module_statements:
            if kind == "star":
                stars.setdefault(path, []).extend(imports)
            for how, referenced in (
                ("import" if kind == "import" else "name", plain),
                ("attribute", attributes),
            ):
                for name in defined.intersection(referenced):
                    references.setdefault(name, []).append(
                        [path, kind, binds, how, imports, name in derives]
                    )
    return Analysis(edges, references, stars, defined, used, missing, holds)


GLOBAL_OPERATIONS = {
    "DELETE_GLOBAL",
    "DELETE_NAME",
    "LOAD_FROM_DICT_OR_GLOBALS",
    "LOAD_GLOBAL",
    "LOAD_NAME",
    "STORE_GLOBAL",
    "STORE_NAME",
}


def mentioned_names(
    code: CodeType,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return the global names, attribute names and strings a function mentions.

    Strings count where they could name an attribute, as in `getattr(obj, "name")`.
    """
    global_names = set()
    attribute_names = set()
    for instruction in dis.get_instructions(code):
        if instruction.opcode not in dis.hasname:
            continue
        if instruction.opname in GLOBAL_OPERATIONS:
            global_names.add(instruction.argval)
        else:
            attribute_names.update(str(instruction.argval).split("."))
    strings = {
        part
        for constant in _strings(code.co_consts)
        for part in constant.split(".")
        if part.isidentifier()
    }
    return frozenset(global_names), frozenset(attribute_names), frozenset(strings)


def _strings(constants: Iterable[object]) -> Iterator[str]:
    """Yield the strings among constants, including those in constant tuples and
    sets, as in `for name in ("a", "b")`."""
    for constant in constants:
        if isinstance(constant, str):
            yield constant
        elif isinstance(constant, tuple | frozenset):
            yield from _strings(constant)
