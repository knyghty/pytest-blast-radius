"""Choose the tests affected by changes since a recorded commit."""

import ast
import builtins
import configparser
import copy
import functools
import inspect
import posixpath
import re
import tomllib
import types
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from fnmatch import fnmatchcase
from typing import Any

from pytest_blast_radius import migrations
from pytest_blast_radius import names
from pytest_blast_radius import recording
from pytest_blast_radius import templates
from pytest_blast_radius import urlconfs
from pytest_blast_radius.storage import Repository

HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)  # Members frameworks look up by names they build, so adding one changes its class:
# Django's view handlers, form field cleaners, middleware hooks, model field
# converters and model hooks, and pytest's marks
# and setup and teardown methods on plain test classes. Also members that builtin
# and library code probe for, as dict() does for keys, files for read, and
# Django's ORM for expressions.
DISPATCHED = frozenset(
    {
        "as_sql",
        "close",
        "delete",
        "fileno",
        "flush",
        "from_db_value",
        "get",
        "get_absolute_url",
        "head",
        "items",
        "keys",
        "natural_key",
        "options",
        "patch",
        "post",
        "put",
        "pytestmark",
        "read",
        "readline",
        "resolve_expression",
        "setup",
        "setup_class",
        "setup_method",
        "teardown",
        "teardown_class",
        "teardown_method",
        "trace",
        "write",
    }
)
DISPATCHED_PREFIXES = ("clean_", "process_")
MIGRATIONS = ["*/migrations/*.py"]
# Names that code finds elsewhere until its module binds them, whose mentions the
# recording drops: builtins, and the module's `__getattr__` and `__dir__`, which
# answer for names it lacks.
SHADOWING = frozenset(
    {name for name in dir(builtins) if not name.startswith("__")}
    | {"__dir__", "__getattr__"}
)
# Settings Django builds something from on first use and keeps, so only the first
# test to need it reads them. Django's own setting_changed receivers reset these.
CACHED_SETTINGS = frozenset(
    {
        "AUTH_PASSWORD_VALIDATORS",
        "AUTH_USER_MODEL",
        "CACHES",
        "DATABASE_ROUTERS",
        "DATABASES",
        "DATETIME_FORMAT",
        "DATETIME_INPUT_FORMATS",
        "DATE_FORMAT",
        "DATE_INPUT_FORMATS",
        "DEBUG",
        "DECIMAL_SEPARATOR",
        "DEFAULT_EXCEPTION_REPORTER_FILTER",
        "DEFAULT_FILE_STORAGE",
        "FIRST_DAY_OF_WEEK",
        "FORMAT_MODULE_PATH",
        "FORM_RENDERER",
        "INSTALLED_APPS",
        "LANGUAGE_CODE",
        "LANGUAGES",
        "LOCALE_PATHS",
        "LOGGING",
        "LOGGING_CONFIG",
        "MEDIA_ROOT",
        "MEDIA_URL",
        "MONTH_DAY_FORMAT",
        "NUMBER_GROUPING",
        "PASSWORD_HASHERS",
        "ROOT_URLCONF",
        "SERIALIZATION_MODULES",
        "SHORT_DATETIME_FORMAT",
        "SHORT_DATE_FORMAT",
        "STATICFILES_DIRS",
        "STATICFILES_FINDERS",
        "STATICFILES_STORAGE",
        "STATIC_ROOT",
        "STATIC_URL",
        "STORAGES",
        "TEMPLATES",
        "THOUSAND_SEPARATOR",
        "TIME_FORMAT",
        "TIME_INPUT_FORMATS",
        "TIME_ZONE",
        "USE_THOUSAND_SEPARATOR",
        "USE_TZ",
        "YEAR_MONTH_FORMAT",
    }
)
# What Django used a URLconf for, in keys like "path:/items/".
URL_USES = {"path", "name", "namespaces", "request"}

type Source = Callable[[str], str | None]


class RunAllError(Exception):
    """Raise when a change could affect any test."""


@dataclass
class Hunk:
    """Old-side lines a diff replaces, and the new-side lines it adds there."""

    start: int
    count: int
    added: list[str] = field(default_factory=list)
    new_start: int = 0
    new_count: int = 0


@dataclass(frozen=True)
class Function:
    """A function's lines in a source file."""

    qualname: str
    first_line: int
    lines: frozenset[int]
    start: int
    end: int
    indent: int


@dataclass
class Affected:
    """What changes affect directly, before following imports."""

    tests: set[str] = field(default_factory=set)
    functions: set[int] = field(default_factory=set)
    # Functions where a change reaches only some statements: the lines of their heads.
    lines: dict[int, frozenset[int]] = field(default_factory=dict)
    files: set[int] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)
    # (path, name, kind): kind is "value", "definition" or "used", as in names.Changes.
    names: set[tuple[str, str, str]] = field(default_factory=set)
    # (path, class, name, kind) for class members, as in names.Changes.
    members: set[tuple[str, str, str, str]] = field(default_factory=set)
    # Template tag libraries whose top level changed.
    libraries: set[str] = field(default_factory=set)
    # (path, name) for top-level names a change removes.
    removed: set[tuple[str, str]] = field(default_factory=set)
    # URLconfs whose top level changed, what changed in their patterns, if only
    # they did, and their new source.
    urls: list[tuple[str, urlconfs.Change | None, str]] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    # Functions that read a changed value, whose import-time runs it may change.
    readers: set[int] = field(default_factory=set)
    # Modules whose top level changed, or computes from a changed value, so their
    # import may now fail.
    top: set[str] = field(default_factory=set)
    # (path, class) for classes that gain or lose special methods, which change
    # their instances wherever they are held, as a new __get__ does.
    special: set[tuple[str, str]] = field(default_factory=set)


def special_changes(
    old: str, new: str, package: str, classes: frozenset[str]
) -> set[str]:
    """Return the classes among `classes` whose bodies bind different special
    names."""
    if not classes:
        return set()
    sides = [
        {
            cls: {
                name
                for _, bound, _, _ in statement.members
                for name in bound
                if name.startswith("__") and name.endswith("__")
            }
            for statement in names.parse_statements(source, package)
            if statement.kind == "class"
            for cls in statement.defined
        }
        for source in (old, new)
    ]
    return {cls for cls in classes if sides[0].get(cls) != sides[1].get(cls)}


def find_changes(
    repository: Repository, base: str, tree: str
) -> dict[str, list[Hunk] | None]:
    """Return files changed between trees, with their hunks or None if unknown."""
    diff = repository.git(
        "diff",
        "-U0",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        base,
        tree,
    )
    hunks = parse_diff(diff)
    names = repository.git("diff", "--name-only", "-z", "--no-renames", base, tree)
    return {name: hunks.get(name) for name in names.split("\0") if name}


def parse_diff(diff: str) -> dict[str, list[Hunk]]:
    """Return the hunks of each file in a `git diff -U0`."""
    changes: dict[str, list[Hunk]] = {}
    header = False
    path = None
    hunks = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            header = True
            path = None
            hunks = None
        elif header and line.startswith(("--- a/", "+++ b/")):
            path = path or line[6:]
        elif match := HUNK.match(line):
            header = False
            if path is not None:
                hunks = changes.setdefault(path, [])
                hunks.append(
                    Hunk(
                        int(match[1]),
                        1 if match[2] is None else int(match[2]),
                        new_start=int(match[3]),
                        new_count=1 if match[4] is None else int(match[4]),
                    )
                )
        elif not header and hunks and line.startswith("+"):
            hunks[-1].added.append(line[1:])
    return changes


def pytest_settings(path: str, source: str | None) -> object:
    """Return a settings file's pytest settings, or its whole text for other files.

    Other tools' settings and dependencies in pyproject.toml and the like change no
    test until installed, which the environment check catches. Of a message file,
    only whether it exists counts: Django keeps which languages have one, while the
    recording credits tests with the files their translations came from.
    """
    name = path.rpartition("/")[2]
    if source is None:
        return None
    if name.endswith(".mo"):
        return True
    try:
        if name == "pyproject.toml":
            return tomllib.loads(source).get("tool", {}).get("pytest")
        if name in {"setup.cfg", "tox.ini"}:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(source)
            section = "tool:pytest" if name == "setup.cfg" else "pytest"
            return dict(parser[section]) if parser.has_section(section) else None
    except (tomllib.TOMLDecodeError, configparser.Error):
        return source
    return source


def matches(path: str, patterns: Iterable[str]) -> bool:
    """Return whether a path or its file name matches any glob."""
    name = path.rpartition("/")[2]
    return any(
        fnmatchcase(path, pattern) or fnmatchcase(name, pattern) for pattern in patterns
    )


class _Normaliser(ast.NodeTransformer):
    """Drop code with no runtime effect: `if TYPE_CHECKING:` and local annotations."""

    in_function = False

    def visit_If(self, node: ast.If) -> ast.AST:
        test = node.test
        if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        ):
            node.body = [ast.Pass()]
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        if self.in_function and isinstance(node.target, ast.Name):
            node.annotation = ast.Constant(None)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._scope(node, in_function=True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._scope(node, in_function=True)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return self._scope(node, in_function=False)

    def _scope(self, node: ast.AST, *, in_function: bool) -> ast.AST:
        outer = self.in_function
        self.in_function = in_function
        node = self.generic_visit(node)
        self.in_function = outer
        return node


def equivalent(old: str, new: str) -> bool:
    """Return whether sources differ only in comments, layout or dead annotations."""
    try:
        trees = [_Normaliser().visit(ast.parse(source)) for source in (old, new)]
    except (SyntaxError, ValueError):
        return False
    return ast.dump(trees[0]) == ast.dump(trees[1])


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return a module's functions by the qualified names their code has."""
    found = {}
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        node, prefix = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found[prefix + child.name] = child
                pending.append((child, f"{prefix}{child.name}.<locals>."))
            elif isinstance(child, ast.ClassDef):
                pending.append((child, f"{prefix}{child.name}."))
            else:
                pending.append((child, prefix))
    return found


def enum_classes(source: str) -> set[str]:
    """Return the top-level classes whose bases look like enums, as `TextChoices`."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            ast.unparse(base).rpartition(".")[2].endswith(("Enum", "Choices", "Flag"))
            for base in node.bases
        )
    }


def invert(mapping: dict[str, list[str]]) -> defaultdict[str, set[str]]:
    """Return the keys that list each value."""
    result: defaultdict[str, set[str]] = defaultdict(set)
    for key, values in mapping.items():
        for value in values:
            result[value].add(key)
    return result


def fixtures(source: str) -> dict[str, list[str]] | None:
    """Return the decorators and parameters of each fixture, by the name tests
    request it by, which decide which tests get it and how."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                not names.decorator_name(decorator)
                .rpartition(".")[2]
                .startswith("fixture")
            ):
                continue
            name = node.name
            if isinstance(decorator, ast.Call):
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(
                        keyword.value, ast.Constant
                    ):
                        name = str(keyword.value.value)
            found.setdefault(name, []).append(
                " ".join(
                    [
                        *map(ast.unparse, node.decorator_list),
                        f"{node.name}({ast.unparse(node.args)})",
                    ]
                )
            )
    return found


def changed_fixtures(old: str, new: str) -> set[str] | None:
    """Return the names of the fixtures a change adds, removes or redefines."""
    before, after = fixtures(old), fixtures(new)
    if before is None or after is None:
        return None
    return {
        name
        for name in before.keys() | after.keys()
        if sorted(before.get(name, [])) != sorted(after.get(name, []))
    }


def annotations_only(old: str, new: str, qualname: str) -> bool:
    """Return whether a change to a function only touches its annotations.

    Calling it then returns the same, so what code built by calling it, such as
    a decorated method, doesn't change.
    """
    dumps = []
    for source in (old, new):
        try:
            function = _functions(ast.parse(source)).get(qualname)
        except (SyntaxError, ValueError):
            return False
        if function is None:
            return False
        stripped = copy.deepcopy(function)
        stripped.returns = None
        stripped.type_params = []
        for argument in ast.walk(stripped.args):
            if isinstance(argument, ast.arg):
                argument.annotation = None
        dumps.append(ast.dump(stripped))
    return dumps[0] == dumps[1]


def indent(line: str) -> int:
    """Return a line's leading whitespace length."""
    return len(line) - len(line.lstrip())


def parse_functions(source: str, path: str) -> tuple[list[Function], set[int]]:
    """Return a source's functions, and the lines that run at module or class level."""
    source_lines = source.splitlines()
    functions = []
    module_lines: set[int] = set()
    pending = [compile(source, path, "exec", dont_inherit=True)]
    while pending:
        code = pending.pop()
        pending.extend(
            constant
            for constant in code.co_consts
            if isinstance(constant, types.CodeType)
        )
        lines = {line for _, _, line in code.co_lines() if line is not None}
        # Annotation functions only cover signature lines, which run at the top level.
        if not code.co_flags & inspect.CO_OPTIMIZED or code.co_name == "__annotate__":
            module_lines |= lines
            continue
        # A statement ends where its last part does, as at a closing bracket.
        ends = {end for _, end, _, _ in code.co_positions() if end is not None}
        functions.append(
            Function(
                code.co_qualname,
                code.co_firstlineno,
                frozenset(lines),
                min(lines),
                max(lines | ends),
                indent(source_lines[code.co_firstlineno - 1]),
            )
        )
    return functions, module_lines


def line_moves(hunks: list[Hunk] | None) -> Callable[[int], int | None]:
    """Return where a changed file's old lines are now, or None for changed lines.

    A file with unknown hunks, such as one whose mode changed, keeps its lines.
    """
    ordered = sorted(hunks or [], key=lambda hunk: hunk.start)

    def move(line: int) -> int | None:
        shift = 0
        for hunk in ordered:
            # A pure insertion goes after its start line; a change replaces lines.
            if line <= hunk.start - (1 if hunk.count else 0):
                break
            if hunk.count and line < hunk.start + hunk.count:
                return None
            shift += hunk.new_count - hunk.count
        return line + shift

    return move


@dataclass(frozen=True)
class _Statement:
    start: int
    end: int
    column: int
    head: frozenset[int]
    definition: bool


class Heads:
    """Find the statement heads a change reaches, to compare with the lines tests ran.

    A statement's head is its whole text for a simple statement, and the lines
    before its first block, such as `if x:`, for a compound one.
    """

    def __init__(self, tree: ast.Module) -> None:
        """Index every statement in a module.

        Docstrings and `global` or `nonlocal` statements run no code of their own,
        but change their whole definition, through `__doc__` or doctests, or how it
        compiles.
        """
        self.statements = []
        whole = {
            id(node.body[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and ast.get_docstring(node, clean=False) is not None
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            if id(node) in whole or isinstance(node, ast.Global | ast.Nonlocal):
                end = node.end_lineno or node.lineno
                lines = frozenset(range(node.lineno, end + 1))
                self.statements.append(
                    _Statement(node.lineno, end, node.col_offset, lines, True)
                )
                continue
            starts = [
                block[0].lineno
                if hasattr(block[0], "lineno")
                else block[0].pattern.lineno
                for field in ("body", "orelse", "finalbody", "handlers", "cases")
                if isinstance(block := getattr(node, field, None), list) and block
            ]
            end = node.end_lineno or node.lineno
            first = min(
                [
                    node.lineno,
                    *(item.lineno for item in getattr(node, "decorator_list", [])),
                ]
            )
            head_end = max(node.lineno, min(starts) - 1) if starts else end
            self.statements.append(
                _Statement(
                    first,
                    end,
                    node.col_offset,
                    frozenset(range(first, head_end + 1)),
                    isinstance(
                        node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                    ),
                )
            )

    def _innermost(self, line: int, column: int | None = None) -> _Statement | None:
        found = [
            statement
            for statement in self.statements
            if statement.start <= line <= statement.end
            and (column is None or statement.column == column)
        ]
        return min(found, key=_size) if found else None

    def changed(self, line: int) -> frozenset[int] | None:
        """Return the heads a changed line reaches, or None for a whole definition."""
        statement = self._innermost(line)
        if statement is None or (statement.definition and line in statement.head):
            return None
        return statement.head

    def inserted(self, after: int, column: int) -> frozenset[int] | None:
        """Return the heads that reach code inserted after a line at a column.

        Code inserted after a statement at its own indentation runs after it; code
        inserted into a block's start runs after the block's head.
        """
        statement = self._innermost(after, column)
        if statement is not None:
            return statement.head
        enclosing = [
            statement
            for statement in self.statements
            if statement.start <= after <= statement.end and statement.column < column
        ]
        if not enclosing or (parent := min(enclosing, key=_size)).definition:
            return None
        return parent.head


def _size(statement: _Statement) -> int:
    return statement.end - statement.start


type Owners = dict[tuple[str, int], frozenset[int] | None]


def find_owners(
    source: str | None, path: str, hunks: list[Hunk] | None
) -> tuple[Owners, set[int] | None]:
    """Return the functions owning changed lines, and the changed top-level lines.

    Each function maps to the statement heads in it that the change reaches, or
    None when the change reaches the whole function, as a signature change does.
    Lines inside a function's span but outside its code, such as comments, belong
    to the innermost such function. A pure insertion belongs to the function it is
    indented into. The top-level lines are None when the source can't be read.
    """
    if source is None:
        return {}, None
    try:
        functions, module_lines = parse_functions(source, path)
        heads = Heads(ast.parse(source))
    except (SyntaxError, ValueError):
        return {}, None
    if hunks is None:
        hunks = [Hunk(1, len(source.splitlines()))]

    def innermost(line: int, below: int | None = None) -> Function | None:
        spans = [
            function
            for function in functions
            if function.start <= line <= function.end
            and (below is None or function.indent < below)
        ]
        if not spans:
            return None
        return min(spans, key=lambda function: function.end - function.start)

    owners: Owners = {}

    def own(function: Function, reached: frozenset[int] | None) -> None:
        key = (function.qualname, function.first_line)
        known = owners.get(key, frozenset())
        owners[key] = None if known is None or reached is None else known | reached

    top_lines = set()
    for hunk in hunks:
        if not hunk.count:
            added = [indent(line) for line in hunk.added if line.strip()]
            # Inserted code belongs to the innermost function it is indented into, or
            # else to the top level, which the new side shows.
            if added and (owner := innermost(hunk.start, min(added))) is not None:
                # A string may be a docstring, which changes the whole function, as
                # `global` and `nonlocal` do.
                whole = any(
                    line.lstrip().startswith(("global ", "nonlocal ", "'", '"'))
                    for line in hunk.added
                )
                own(owner, None if whole else heads.inserted(hunk.start, min(added)))
            # Code inside a function's span at any indent, as in a multi-line
            # string, belongs to it too.
            elif (
                added
                and (enclosing := innermost(hunk.start)) is not None
                and hunk.start < enclosing.end
            ):
                own(enclosing, None)
            if added:
                continue
        lines = (
            range(hunk.start, hunk.start + hunk.count)
            if hunk.count
            else (hunk.start, hunk.start + 1)
        )
        for line in lines:
            direct = [function for function in functions if line in function.lines]
            if not direct and (enclosing := innermost(line)) is not None:
                direct = [enclosing]
            for function in direct:
                own(function, heads.changed(line))
            # What an insertion adds at the top level shows on the new side.
            if hunk.count and (line in module_lines or not direct):
                top_lines.add(line)
    return owners, top_lines


def top_level_lines(source: str, path: str, hunks: list[Hunk]) -> set[int]:
    """Return the new-side changed lines outside any function's code."""
    functions, module_lines = parse_functions(source, path)
    return {
        line
        for hunk in hunks
        for line in range(hunk.new_start, hunk.new_start + hunk.new_count)
        if line in module_lines
        or not any(function.start <= line <= function.end for function in functions)
    }


def changed_names(  # noqa: PLR0913
    old: str,
    new: str,
    path: str,
    *,
    package: str,
    hunks: list[Hunk],
    old_lines: set[int],
    loaded: set[str],
    registries: Iterable[str] = (),
    registering: bool = True,
) -> names.Changes | None:
    """Return the top-level names a change rebinds, or None if it does more, as
    rebinding a name that code found elsewhere does."""
    try:
        new_lines = top_level_lines(new, path, hunks)
    except (SyntaxError, ValueError):
        return None
    changed = names.changed_names(
        old,
        new,
        package,
        (old_lines, new_lines),
        loaded,
        registries=registries,
        registering=registering,
    )
    if changed is None or SHADOWING.isdisjoint(
        changed.values | changed.definitions | changed.bindings
    ):
        return changed
    return None


class Selector:
    """Index a recorded run and select the tests affected by changes."""

    def __init__(
        self,
        data: dict[str, Any],
        full_run: list[str],
        also_run: list[tuple[str, str]],
        python_files: Iterable[str] = ("test_*.py", "*_test.py"),
    ) -> None:
        """Build lookups from a recorded run. `python_files` are the globs pytest
        collects test files by."""
        files = data["files"]
        self.full_run, self.also_run = full_run, also_run
        self.python_files = list(python_files)
        self.tests: dict[str, list[Any]] = data["tests"]
        self.file_index = {path: index for index, path in enumerate(files)}
        self.function_files = [files[file] for file, _, _ in data["functions"]]
        self.function_qualnames = [qualname for _, qualname, _ in data["functions"]]
        self.function_lines = [line for _, _, line in data["functions"]]
        self.function_index = {
            (files[file], qualname, line): index
            for index, (file, qualname, line) in enumerate(data["functions"])
        }
        self.test_files: defaultdict[str, set[str]] = defaultdict(set)
        self.line_sets = [frozenset(lines) for lines in data["line_sets"]]
        for nodeid, (path, *_) in self.tests.items():
            self.test_files[files[path]].add(nodeid)
        self.index_imports(data)
        self.import_functions: defaultdict[int, set[str]] = defaultdict(set)
        self.import_files: defaultdict[int, set[str]] = defaultdict(set)
        for module, function_indices, file_indices in data["imports"]:
            for index in function_indices:
                self.import_functions[index].add(files[module])
            for index in file_indices:
                self.import_files[index].add(files[module])
        self.session_functions = set(data["session"][0])
        self.session_files = set(data["session"][1])
        self.module_paths = {
            name: files[file] for name, file in data["modules"].items()
        }
        self.module_names = {path: name for name, path in self.module_paths.items()}
        self.packages = {
            path: name if path.endswith("__init__.py") else name.rpartition(".")[0]
            for path, name in self.module_names.items()
        }
        self.loaded_names = set(data["loaded"])
        self.libraries: dict[str, list[str]] = data["libraries"]
        self.builtins = set(data["builtins"])
        # Enum classes, as "path:Name", including those whose bases don't look like
        # enums.
        self.enums: list[str] = data["enums"]
        self.settings_file: str | None = data["settings"]
        self.covered = set(data["covered"])
        self.references: dict[str, list[list[Any]]] = data["references"]
        self.stars: dict[str, list[str]] = data["stars"]
        # The modules that tried to import each project module that didn't load.
        self.missing = invert(data["missing"])
        self.global_mentions: defaultdict[str, set[int]] = defaultdict(set)
        self.attribute_mentions: defaultdict[str, set[int]] = defaultdict(set)
        self.string_mentions: defaultdict[str, set[int]] = defaultdict(set)
        for index, global_names, attribute_names, strings in data["names"]:
            for name in global_names:
                self.global_mentions[name].add(index)
            for name in attribute_names:
                self.attribute_mentions[name].add(index)
            for name in strings:
                self.string_mentions[name].add(index)
        self.index_classes(data)
        self.members: defaultdict[tuple[str, str], set[int]] = defaultdict(set)
        # Modules a framework used for tests, and the keys tests were credited with
        # for them: line 0 and no name, or what the framework used, after a colon.
        self.framework_modules: defaultdict[str, dict[int, str]] = defaultdict(dict)
        # Each function by its qualified name, as for a method `Class.name`.
        self.methods: defaultdict[tuple[str, str], set[int]] = defaultdict(set)
        for index, (file, qualname, line) in enumerate(data["functions"]):
            self.members[files[file], qualname.partition(".")[0]].add(index)
            self.methods[files[file], qualname].add(index)
            if not line and (not qualname or qualname.partition(":")[0] in URL_USES):
                self.framework_modules[files[file]][index] = qualname
        self.index_modules()

    def index_modules(self) -> None:
        """Index URLconfs, the keys of the tests that used URLs, the tests that
        set up a database with migrations, where module paths start, and the
        fixtures tests requested."""
        # Test files a selection needs collected, to see they still import.
        self.collect: set[str] = set()
        self.fixture_keys: defaultdict[str, list[tuple[str, int]]] = defaultdict(list)
        for (file, qualname, _), index in self.function_index.items():
            if qualname.startswith(recording.FIXTURE):
                self.fixture_keys[qualname.removeprefix(recording.FIXTURE)].append(
                    (file, index)
                )
        self.url_modules = {
            path
            for path, keys in self.framework_modules.items()
            if any(detail for detail in keys.values())
        }
        self.url_users = {
            index for path in self.url_modules for index in self.framework_modules[path]
        }
        migration_files = {
            index
            for path, index in self.file_index.items()
            if matches(path, MIGRATIONS)
        }
        migration_functions = {
            index
            for index, path in enumerate(self.function_files)
            if matches(path, MIGRATIONS)
        }
        self.database_users = {
            nodeid
            for nodeid, (_, functions, files, _) in self.tests.items()
            if not migration_files.isdisjoint(files)
            or not migration_functions.isdisjoint(functions)
        }
        self.source_roots = set()
        for name, path in self.module_paths.items():
            relative = name.replace(".", "/")
            for suffix in (".py", "/__init__.py"):
                if path.endswith(relative + suffix):
                    self.source_roots.add(path.removesuffix(relative + suffix))

    def index_classes(self, data: dict[str, Any]) -> None:
        """Index the names each class inherits and which of those record their
        calls, each class's ancestors and subclasses, and which classes are
        plain."""
        files = data["files"]
        sets = [frozenset(names) for names in data["inherited"]]
        self.inherited: dict[tuple[str, str], frozenset[str]] = {}
        self.tracked: dict[tuple[str, str], frozenset[str]] = {}
        self.plain: set[tuple[str, str]] = set()
        self.ancestors: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.descendants: defaultdict[tuple[str, str], set[tuple[str, str]]] = (
            defaultdict(set)
        )
        for file, qualname, inherited, ancestors, plain, tracked in data["classes"]:
            key = (files[file], qualname)
            self.inherited[key] = sets[inherited]
            self.tracked[key] = sets[tracked]
            if plain:
                self.plain.add(key)
            self.ancestors[key] = [(files[path], name) for path, name in ancestors]
            for ancestor in self.ancestors[key]:
                self.descendants[ancestor].add(key)

    def select(
        self,
        changes: dict[str, list[Hunk] | None],
        old_source: Source,
        new_source: Source,
    ) -> set[str]:
        """Return the recorded tests affected by changes, or raise RunAllError."""
        affected = Affected()
        for path, hunks in changes.items():
            for pattern, test_pattern in self.also_run:
                if matches(path, [pattern]):
                    for test_file, nodeids in self.test_files.items():
                        if matches(test_file, [test_pattern]):
                            affected.tests |= nodeids
            # Setting up the test database runs every migration, so a changed one
            # reaches the tests that set it up, if the recording saw that happen,
            # and the tests that used the models whose tables it changes.
            if matches(path, MIGRATIONS) and self.database_users:
                affected.tests |= self.database_users
                self.migration_change(
                    path, old_source(path), new_source(path), affected
                )
            # The recording credits tests with the settings they read.
            elif (
                path != self.settings_file
                and matches(path, self.full_run)
                and pytest_settings(path, old_source(path))
                != pytest_settings(path, new_source(path))
            ):
                msg = f"{path} changed"
                raise RunAllError(msg)
            if old_source(path) is None:
                self.listed(path, affected, old_source)
            elif new_source(path) is None:
                self.listed(path, affected, new_source)
            self.contents(path, [old_source(path), new_source(path)], affected)
            if path.endswith(".py"):
                self.python_change(
                    path, hunks, old_source(path), new_source(path), affected
                )
            elif (index := self.file_index.get(path)) is not None:
                if index in self.session_files:
                    msg = f"{path} is read outside tests"
                    raise RunAllError(msg)
                self.data_change(
                    path,
                    index,
                    hunks,
                    (old_source(path) or "", new_source(path) or ""),
                    affected,
                )
        texts = functools.cache(
            lambda: {
                index: new_source(file) or ""
                for file, index in self.file_index.items()
                if not file.endswith((".py", "/"))
            }
        )
        self.follow_members(affected, old_source, texts)
        self.follow_removed(affected, new_source)
        self.follow_names(affected)
        self.follow_urls(affected, new_source)
        self.follow_import_time(affected, old_source)
        self.collect = self.importing_tests(affected.top | affected.modules)
        modules = self.closure(affected.modules, affected.reasons, new_source)
        affected.libraries |= {
            path for path in modules if path in self.libraries or path in self.builtins
        }
        self.follow_libraries(affected, texts)
        # Tests that imported a changed module saw its top level run. For a module a
        # framework uses, that is only whichever test loaded it first, and the
        # framework's own credits say which tests use it.
        files = affected.files | {
            self.file_index[path]
            for path in modules
            if path in self.file_index and path not in self.framework_modules
        }
        functions = affected.functions | {
            index for index, path in enumerate(self.function_files) if path in modules
        }
        partly = {
            index: lines
            for index, lines in affected.lines.items()
            if index not in functions
        }
        return affected.tests | {
            nodeid
            for nodeid, (_, function_indices, file_indices, ran) in self.tests.items()
            if not functions.isdisjoint(function_indices)
            or not files.isdisjoint(file_indices)
            or (
                not partly.keys().isdisjoint(function_indices)
                and self.ran_any(partly, function_indices, ran)
            )
        }

    def index_imports(self, data: dict[str, Any]) -> None:
        """Index which modules import, hold and load which."""
        # The modules that loaded, and the project files that loaded each module by
        # name, as import_module() does.
        self.loaded, self.loaders = set(data["edges"]), data.get("loaders", {})
        loads = data.get("loads", {})
        # A module's top level loading one through project functions imports it.
        self.imported = {
            path: {*imported, *loads.get(path, ())}
            for path, imported in data["edges"].items()
        }
        # The modules whose objects each one binds, as a loader may return them.
        self.holds = {
            path: {*data.get("holds", {}).get(path, ()), *loads.get(path, ())}
            for path in data["edges"]
        }
        self.importers: defaultdict[str, set[str]] = defaultdict(set)
        for path, imported in self.imported.items():
            for target in imported:
                self.importers[target].add(path)
        # What importing() and holding() found.
        self.reached: dict[str, set[str]] = {}
        self.held: dict[str, set[str]] = {}

    def importing_tests(self, modules: set[str]) -> set[str]:
        """Return the test files that import any of these modules, at any depth.

        Collecting them shows whether the modules still import, as a value they
        compute from, or an import cycle, may now raise.
        """
        reached = set(modules).union(*map(self.importing, modules))
        return reached & self.test_files.keys()

    def importing(self, path: str) -> set[str]:
        """Return a module and the modules that import it, at any depth."""
        reached = self.reached.get(path)
        if reached is None:
            reached = self.reached[path] = {path}
            pending = [path]
            while pending:
                for importer in self.importers.get(pending.pop(), ()):
                    if importer not in reached:
                        reached.add(importer)
                        pending.append(importer)
        return reached

    def source_read(self, path: str, affected: Affected) -> None:
        """Add the tests that read a changed Python file as data, which see any
        change, even to a comment."""
        index = self.function_index.get((path, recording.SOURCE, 0))
        if index is None:
            return
        if index in self.session_functions:
            msg = f"{path} is read outside tests"
            raise RunAllError(msg)
        affected.functions.add(index)

    def listed(self, path: str, affected: Affected, other: Source) -> None:
        """Add the tests that listed a directory holding an added or removed file,
        as through a glob, which may find a different set of files.

        A file changes only its own directory's listing. A directory that `other`,
        the tree without the file, lacks comes or goes with it, which changes
        whether it exists and its parent's listing too.
        """
        # Listings end in a slash, and the root's is "/"; a check for a directory,
        # as os.path.isdir() makes, has none.
        directory = path.rpartition("/")[0]
        keys = [f"{directory}/"]
        while directory and other(directory) is None:
            keys.append(directory)
            directory = directory.rpartition("/")[0]
            keys.append(f"{directory}/")
        for key in keys:
            if (index := self.file_index.get(key)) is not None:
                if index in self.session_files:
                    msg = f"{key} is listed outside tests"
                    raise RunAllError(msg)
                affected.files.add(index)
                # A listing at import time, as a module-level glob makes.
                affected.modules |= self.import_files[index]

    def contents(
        self, path: str, sources: list[str | None], affected: Affected
    ) -> None:
        """Add what used files under a changed path that holds files, as a
        submodule or a symlink to a directory does, and under where such a link
        pointed, since the recording resolves links."""
        directories = {path}
        for source in sources:
            # A symlink's content is its target.
            if source and "\n" not in source and not source.startswith("/"):
                directories.add(posixpath.normpath(posixpath.join(path, "..", source)))
        for directory in directories:
            prefix = f"{directory}/"
            affected.files |= {
                index
                for file, index in self.file_index.items()
                if file.startswith(prefix)
            }
            affected.modules |= {
                file for file in self.module_names if file.startswith(prefix)
            }

    def added_module(self, path: str, affected: Affected) -> None:
        """Add the modules that tried to import an added module, which now loads,
        and the module of that name loaded from elsewhere, which it may replace, as
        a package replaces a module or an earlier `sys.path` entry wins."""
        for name in self.module_names_for(path):
            if (other := self.module_paths.get(name, path)) != path:
                affected.modules.add(other)
                affected.reasons.setdefault(
                    other, f"{path} may replace {other} as {name}"
                )
            for importer in self.missing.get(name, ()):
                affected.modules.add(importer)
                affected.reasons.setdefault(
                    importer, f"{importer} tried to import {name}, which now exists"
                )

    def module_names_for(self, path: str) -> list[str]:
        """Return the names a project file imports as, from where module paths
        start, whether or not it loaded."""
        return [
            path.removeprefix(root)
            .removesuffix(".py")
            .removesuffix("/__init__")
            .replace("/", ".")
            for root in self.source_roots
            if path.startswith(root)
        ]

    def fixture_change(
        self, path: str, changed: set[str] | None, affected: Affected
    ) -> None:
        """Add the tests that request a fixture a change adds, removes or
        redefines, where the fixture's file reaches them.

        Tests request fixtures by name, so they don't mention them in code.
        """
        if changed is None:
            return
        directory, _, name = path.rpartition("/")
        prefix = f"{directory}/" if directory else ""
        for fixture in changed:
            for file, index in self.fixture_keys[fixture]:
                if (
                    file == path
                    or (name == "conftest.py" and file.startswith(prefix))
                    # A plugin module's fixtures reach every test.
                    or (name != "conftest.py" and path not in self.test_files)
                ):
                    affected.functions.add(index)

    def migration_change(
        self, path: str, old: str | None, new: str | None, affected: Affected
    ) -> None:
        """Add the users of the models a changed migration's operations change."""
        before, after = migrations.models(old), migrations.models(new)
        if before is None or after is None:
            msg = f"{path} runs code or changes more than models"
            raise RunAllError(msg)
        app = path.rpartition("/migrations/")[0] + "/"
        models = before | after
        for class_path, cls in self.ancestors:
            if (
                class_path.startswith(app)
                and "/migrations/" not in class_path
                and cls.lower() in models
            ):
                affected.functions |= self.class_users(class_path, cls)

    def reaching(
        self,
        nodeids: Iterable[str],
        changed: Iterable[str],
        failed_in: Mapping[str, Iterable[str]] | None = None,
        new_source: Source | None = None,
    ) -> set[str]:
        """Return the tests that ran a module importing a changed one, at any depth.

        A failed test's recording stops where it failed, so what selection assumes
        about the rest of the run doesn't hold. A module it ran may now load, for
        example, because a name it refers to has been added. A module that failed
        to load left no record of what it imports, so a test is reached too when a
        file its failure's traceback went through, `failed_in`, or a module it used
        that never loaded, or one either now imports, is.
        """
        reached: set[str] = set()
        stack = [path for path in changed if path.endswith(".py")]
        while stack:
            path = stack.pop()
            if path not in reached:
                reached.add(path)
                stack.extend(self.importers.get(path, ()))
        files = {self.file_index[path] for path in reached if path in self.file_index}
        functions = {
            index for index, path in enumerate(self.function_files) if path in reached
        }
        # Keys in modules that never finished loading, as a URLconf naming a view
        # that didn't exist, whose imports the recording doesn't know either.
        unloaded = {
            index: path
            for index, path in enumerate(self.function_files)
            if path.endswith(".py") and path not in self.loaded
        }

        @functools.cache
        def reaches(path: str) -> bool:
            if path in reached:
                return True
            source = (new_source(path) if new_source is not None else None) or ""
            # The module may never have loaded, which would have noted its package.
            packages = (
                {self.packages[path]}
                if path in self.packages
                else {
                    name if path.endswith("/__init__.py") else name.rpartition(".")[0]
                    for name in self.module_names_for(path)
                }
                or {""}
            )
            try:
                statements = [
                    statement
                    for package in packages
                    for statement in names.parse_statements(source, package)
                ]
            except (SyntaxError, ValueError):
                return True
            return any(
                self.module_paths.get(module) in reached
                for statement in statements
                for module in statement.modules
            )

        return {
            nodeid
            for nodeid in nodeids
            if nodeid in self.tests
            and (
                not files.isdisjoint(self.tests[nodeid][2])
                or not functions.isdisjoint(self.tests[nodeid][1])
                or any(
                    reaches(path)
                    for path in {
                        *(failed_in or {}).get(nodeid, ()),
                        *(
                            unloaded[index]
                            for index in self.tests[nodeid][1]
                            if index in unloaded
                        ),
                    }
                    if path.endswith(".py")
                )
            )
        }

    def data_change(
        self,
        path: str,
        index: int,
        hunks: list[Hunk] | None,
        sources: tuple[str, str],
        affected: Affected,
    ) -> None:
        """Add what a change to a file tests read affects, from its old and new
        text.

        Tests that rendered a changed template need only run if they rendered a
        node the change reaches; tests that read it otherwise still run.
        """
        affected.modules |= self.import_files[index]
        template = self.function_index.get((path, templates.TEMPLATE, 1))
        # Engines such as Jinja compile a template to code under its path, and
        # tests run that code without reading the file again.
        affected.functions |= {
            function
            for function, file in enumerate(self.function_files)
            if file == path and function != template
        }
        reached = (
            None
            if template is None
            or hunks is None
            or templates.reparses(
                sources[1], [(hunk.new_start, hunk.new_count) for hunk in hunks]
            )
            else templates.heads(
                sources[0], [(hunk.start, hunk.count) for hunk in hunks]
            )
        )
        if template is None or reached is None:
            affected.files.add(index)
            return
        affected.lines[template] = affected.lines.get(template, frozenset()) | reached
        affected.tests |= {
            nodeid
            for nodeid, (_, functions, files, _) in self.tests.items()
            if index in files and template not in functions
        }

    def ran_any(
        self,
        partly: dict[int, frozenset[int]],
        function_indices: list[int],
        ran: list[list[int]],
    ) -> bool:
        """Return whether a test ran a changed head in a partly changed function.

        A test credited with a function without its lines, such as through a fixture
        or an import, counts as having run all of them.
        """
        lines = {pair[0]: pair[1] for pair in ran}
        return any(
            index not in lines or not heads.isdisjoint(self.line_sets[lines[index]])
            for index, heads in partly.items()
            if index in function_indices
        )

    def python_change(
        self,
        path: str,
        hunks: list[Hunk] | None,
        old: str | None,
        new: str | None,
        affected: Affected,
    ) -> None:
        """Add what a change to a Python file affects."""
        self.source_read(path, affected)
        if old is not None and new is not None and equivalent(old, new):
            return
        # Pytest loads every conftest.py it finds, without anything importing it.
        if path.rpartition("/")[2] == "conftest.py" and (old is None or new is None):
            msg = f"{path} changed"
            raise RunAllError(msg)
        if old is not None and new is not None:
            self.fixture_change(path, changed_fixtures(old, new), affected)
        if old is None:
            self.added_module(path, affected)
        owners, top_lines = find_owners(old, path, hunks)
        if (
            top_lines is None
            or top_lines
            or any(hunk.new_count for hunk in hunks or [])
        ):
            self.top_level_change(
                path, hunks, old, new, top_lines=top_lines, affected=affected
            )
        for (qualname, line), reached in owners.items():
            index = self.function_index.get((path, qualname, line))
            if index is None:
                continue
            if index in self.session_functions:
                msg = f"{qualname} in {path} runs outside tests"
                raise RunAllError(msg)
            if reached is None:
                affected.functions.add(index)
            else:
                affected.lines[index] = affected.lines.get(index, frozenset()) | reached
            if not (
                self.import_functions[index]
                and old is not None
                and new is not None
                and annotations_only(old, new, qualname)
            ):
                affected.modules |= self.import_functions[index]

    def top_level_change(  # noqa: PLR0913
        self,
        path: str,
        hunks: list[Hunk] | None,
        old: str | None,
        new: str | None,
        *,
        top_lines: set[int] | None,
        affected: Affected,
    ) -> None:
        """Add what a change to a Python file's top level affects.

        A change to a template tag library also changes the templates that load
        it, through what it registers. A change to a URLconf waits for
        follow_urls, which checks it still loads.
        """
        affected.top.add(path)
        if old is None or new is None or hunks is None or top_lines is None:
            affected.modules.add(path)
            if path in self.url_modules:
                affected.urls.append((path, None, ""))
            return
        library = path in self.libraries or path in self.builtins
        changed = changed_names(
            old,
            new,
            path,
            package=self.packages.get(path, ""),
            hunks=hunks,
            old_lines=top_lines,
            loaded=self.loaded_names,
            registries=names.libraries(old) | names.libraries(new) if library else (),
            # Decorators on tests wrap or mark them, as `override_settings` does.
            registering=path not in self.test_files,
        )
        if changed == names.Changes():
            return
        if path == self.settings_file:
            self.settings_change(path, new, changed, affected)
            return
        # `__all__` decides what importing everything from the module binds.
        if changed is None or "__all__" in changed.values | changed.definitions:
            module = self.module_names.get(path)
            for importer, modules in self.stars.items():
                if module in modules:
                    affected.modules.add(importer)
                    affected.reasons.setdefault(
                        importer, f"{importer} imports everything from {path}"
                    )
        if library:
            affected.libraries.add(path)
        if path in self.url_modules:
            url_change = urlconfs.changes(old, new)
            affected.urls.append((path, url_change, new))
            # Other pattern lists may be imported and included elsewhere.
            if url_change is not None and (
                changed is None or changed.values <= {"urlpatterns"}
            ):
                return
        if changed is None or (not library and self.read_by_framework(path, changed)):
            affected.modules.add(path)
        else:
            affected.names |= {
                (path, name, kind)
                for kind, changed_names in (
                    ("value", changed.values),
                    ("definition", changed.definitions),
                    ("used", changed.used),
                    ("binding", changed.bindings),
                )
                for name in changed_names
            }
            # An enum's members are data that what holds it copies, as Django's
            # `choices=Status` does, so a change to one changes values built from it.
            enums = (
                enum_classes(old)
                | enum_classes(new)
                | {
                    name.partition(":")[2]
                    for name in self.enums
                    if name.partition(":")[0] == path
                }
            )
            affected.names |= {
                (path, name, "value")
                for name in changed.definitions & enums
                | {member[0] for member in changed.members} & enums
            }
            affected.members |= {
                (path, *member) for member in changed.members if member[0] not in enums
            }
            affected.special |= {
                (path, cls)
                for cls in special_changes(
                    old,
                    new,
                    self.packages.get(path, ""),
                    changed.definitions | {member[0] for member in changed.members},
                )
            }
            affected.removed |= {(path, name) for name in changed.removed}

    def settings_change(
        self,
        path: str,
        new: str,
        changed: names.Changes | None,
        affected: Affected,
    ) -> None:
        """Add the tests that read changed settings.

        A setting computed from a changed name changes too. Settings read outside
        tests, such as INSTALLED_APPS, and changes beyond plain bindings could
        change anything.
        """
        if changed is None:
            msg = f"{path} changed"
            raise RunAllError(msg)
        settings = set(changed.values | changed.definitions | changed.bindings)
        statements = names.parse_statements(new)
        grown = True
        while grown:
            grown = False
            for statement in statements:
                if not settings.isdisjoint(
                    statement.names | statement.attributes
                ) and not settings.issuperset(statement.defined):
                    settings |= statement.defined
                    grown = True
        for name in settings:
            self.setting_change(path, name, affected)

    def setting_change(self, path: str, name: str, affected: Affected) -> None:
        """Add the tests that read a changed setting, and the modules whose import
        read it, as a top-level `PAGE = settings.PAGE` does."""
        index = self.function_index.get((path, f"setting:{name}", 0))
        if index is None:
            return
        if index in self.session_functions:
            msg = f"{path} changed {name}, which is read outside tests"
            raise RunAllError(msg)
        if name in CACHED_SETTINGS:
            msg = f"{path} changed {name}, which Django reads once and keeps"
            raise RunAllError(msg)
        affected.functions.add(index)
        affected.modules |= self.import_functions[index]

    def follow_libraries(
        self, affected: Affected, texts: Callable[[], dict[int, str]]
    ) -> None:
        """Add the templates that load changed template tag libraries.

        Every template loads a builtin library; others load it by a name the
        recording knows, or else by its module's name.
        """
        for path in affected.libraries:
            if path in self.builtins:
                affected.files |= set(texts())
                continue
            loads = self.libraries.get(path) or [path.rpartition("/")[2][:-3]]
            load = re.compile(
                r"{%\s*load\s[^%]*\b(?:" + "|".join(map(re.escape, loads)) + r")\b"
            )
            affected.files |= {
                index for index, text in texts().items() if load.search(text)
            }

    def follow_removed(self, affected: Affected, new_source: Source) -> None:
        """Add the modules that no longer load because a name they use is gone.

        A module's top level breaks when it imports a removed name, reads it as an
        attribute of the module it came from, or, in the same module, uses it by
        name, unless the module stopped doing so too. Everything that imports a
        broken module breaks with it.
        """
        for path, name in affected.removed:
            module = self.module_names.get(path, "")
            # Importers may import it inside `if` or `try`, or import everything.
            candidates = {path, *self.importers.get(path, ())} | {
                reference_path
                for reference_path, _, _, how, _, _ in self.references.get(name, [])
                if how == "attribute" and self.sees(reference_path, path)
            }
            for reference_path in sorted(candidates):
                if names.needs(
                    new_source(reference_path) or "",
                    self.packages.get(reference_path, ""),
                    own=reference_path == path,
                    module=module,
                    name=name,
                ):
                    affected.modules.add(reference_path)
                    affected.reasons.setdefault(
                        reference_path,
                        f"{reference_path} uses {name}, which {path} no longer has",
                    )
                    if reference_path in self.url_modules:
                        affected.urls.append((reference_path, None, ""))

    def follow_urls(self, affected: Affected, new_source: Source) -> None:
        """Add the tests changed URLconfs reach.

        A URLconf that may fail to load, or changes more than its patterns, may
        change every URL: Django loads them all at once. A change to its patterns
        that loads reaches the tests url_keys finds.
        """
        for path, change, new in affected.urls:
            if change is None or urlconfs.unresolved(
                new,
                self.packages.get(path, ""),
                change,
                lambda name: self.module_source(name, new_source),
            ):
                keys = self.url_users
            else:
                keys = self.url_keys(path, change)
            affected.functions |= keys
            # URLs a module used as it loaded, as `URL = reverse("x")` in a test
            # module, reach the tests that imported it.
            for index in keys:
                affected.modules |= self.import_functions.get(index, set())

    def module_source(self, name: str, new_source: Source) -> str | None:
        """Return a project module's new source, or None outside the project."""
        known = self.module_paths.get(name)
        candidates = [known] if known else []
        relative = name.replace(".", "/")
        candidates += [
            f"{root}{relative}{suffix}"
            for root in self.source_roots
            for suffix in (".py", "/__init__.py")
        ]
        for candidate in candidates:
            source = new_source(candidate)
            if source is not None:
                return source
        return None

    def url_keys(self, path: str, change: urlconfs.Change) -> set[int]:
        """Return the keys of tests a change to a URLconf's patterns reaches.

        They resolved a path a changed pattern could serve, reversed a changed name,
        or, where an include changed, reversed any name through it. Keys for the
        whole module, as for reversing a view, always count.
        """
        keys = set()
        for index, detail in self.framework_modules[path].items():
            kind, _, value = detail.partition(":")
            if (
                not detail
                or (kind == "path" and any(p.search(value) for p in change.paths))
                or (kind == "name" and (change.includes or value in change.names))
                or (kind == "namespaces" and change.includes)
            ):
                keys.add(index)
        return keys

    def read_by_framework(self, path: str, changed: names.Changes) -> bool:
        """Return whether a framework reads changed names, rather than project code.

        Pytest reads hooks, `pytestmark` and `collect_ignore` in conftest and test
        modules; it finds fixtures through the tests that use them. Other frameworks
        read the names in a module nothing imports, such as urlpatterns, apart from
        classes the Django hooks credit.
        """
        rebound = changed.values | changed.definitions | changed.bindings
        if path.rpartition("/")[2] == "conftest.py" or path in self.test_files:
            return any(
                name == "pytestmark" or name.startswith(("pytest_", "collect_ignore"))
                for name in rebound
            )
        return self.unimported(path) and not rebound <= self.covered

    def follow_members(
        self,
        affected: Affected,
        old_source: Source,
        texts: Callable[[], dict[int, str]],
    ) -> None:
        """Add the tests that may see changed class members.

        Seeing a member takes the class or one of its instances, and a lookup of
        the member's name. So a test may see it when it used the class, and ran
        code mentioning the name as an attribute or a string, or read a template
        mentioning it. A member that overrides an inherited one, or that a framework
        finds by a name it builds, changes the class, as any other class change
        does. So does an attribute of a class that is not plain, or that a subclass
        or top-level code reads as the project loads.
        """
        if not affected.members:
            return

        @functools.cache
        def recorded_statements(path: str) -> list[names.Statement]:
            # The recorded references come from these sources, which parsed then.
            return names.parse_statements(old_source(path) or "")

        for path, cls, member, kind in affected.members:
            # Code that ran as modules loaded may have read an attribute, as an
            # inherited classmethod or `__init_subclass__` does.
            if kind == "attribute":
                related = {
                    key[0]
                    for key in [
                        (path, cls),
                        *self.ancestors.get((path, cls), []),
                        *self.descendants[path, cls],
                    ]
                }
                affected.readers |= {
                    index
                    for index in self.attribute_mentions[member]
                    | self.string_mentions[member]
                    if (
                        index in self.import_functions
                        or index in self.session_functions
                    )
                    and any(
                        self.sees(self.function_files[index], other, index)
                        for other in related
                    )
                }
            calls = self.calls(path, cls, member) if kind == "method" else None
            if (calls is None and self.overrides(path, cls, member)) or (
                kind == "attribute"
                and not self.keeps_attribute(path, cls, member, recorded_statements)
            ):
                affected.names.add((path, cls, "definition"))
                continue
            # Code that looks the name up, Django calling the method it overrides,
            # or anything calling a project ancestor's method it overrides.
            finders = (
                self.attribute_mentions[member]
                | self.string_mentions[member]
                | (calls or set())
                | {
                    index
                    for ancestor_path, ancestor in self.ancestors.get((path, cls), [])
                    for index in self.methods[ancestor_path, f"{ancestor}.{member}"]
                }
            )
            word = re.compile(rf"\b{member}\b")
            templates = {index for index, text in texts().items() if word.search(text)}
            users = self.class_users(path, cls) | (calls or set())
            # Code reads attributes in more ways than by name, as through vars()
            # or a descriptor's __set_name__, so every user may see one.
            affected.tests |= {
                nodeid
                for nodeid, (_, functions, files, _) in self.tests.items()
                if not users.isdisjoint(functions)
                and (
                    kind == "attribute"
                    or not finders.isdisjoint(functions)
                    or not templates.isdisjoint(files)
                )
            }

    def class_users(
        self, path: str, cls: str, seen: set[tuple[str, str]] | None = None
    ) -> set[int]:
        """Return the functions and class keys that show a test used a class.

        A test used it when the recording credits the class, which it does for the
        classes of instances whose methods ran and for every class in their MRO, or
        when the test ran code referring to the class or one of its subclasses, or
        to a top-level name assigned from it, as `CONF` in `CONF = Conf()` or
        `CONF = make()` where `make` refers to it, whose instance may be used
        without running its methods, or that read the migration state of a model
        holding an instance, at any depth, as held_states finds.
        """
        seen = seen if seen is not None else set()
        seen.add((path, cls))
        users = set()
        for class_path, name in [(path, cls), *self.descendants[path, cls]]:
            key = self.function_index.get((class_path, name, 0))
            mentions = self.mentions(name, class_path)
            users |= (
                ({key} if key is not None else set())
                | self.members[class_path, name]
                | self.string_mentions[name]
                | mentions
            )
            # Top-level functions that refer to it, or to such functions, may build
            # instances at import.
            makers = {(class_path, name)}
            pending = [mentions]
            while pending:
                for index in pending.pop():
                    maker = (self.function_files[index], self.function_qualnames[index])
                    if "." not in maker[1] and maker not in makers:
                        makers.add(maker)
                        pending.append(self.mentions(maker[1], maker[0]))
            for _, maker in makers:
                for reference_path, kind, defined, how, _, _ in self.references.get(
                    maker, []
                ):
                    # As `CONF = Conf()`, `def f(conf=Conf())` or a class body's
                    # `conf = Conf()`.
                    if kind != "import" and how != "import":
                        for holder in defined:
                            users |= self.mentions(holder, reference_path)
            users |= self.held_states(class_path, name, seen)
        return users

    def held_states(self, path: str, cls: str, seen: set[tuple[str, str]]) -> set[int]:
        """Return the keys of tests that read the migration state of a class holding
        an instance, at any depth, as of a model whose StreamField holds a block
        that holds another, which makemigrations deconstructs."""
        keys = set()
        for reference_path, kind, defined, how, _, _ in self.references.get(cls, []):
            if kind != "class" or how == "import":
                continue
            for holder in defined:
                if (reference_path, holder) in seen:
                    continue
                seen.add((reference_path, holder))
                state = (reference_path, f"{recording.STATE}{holder}", 0)
                if (key := self.function_index.get(state)) is not None:
                    keys.add(key)
                keys |= self.held_states(reference_path, holder, seen)
        return keys

    def mentions(self, name: str, path: str) -> set[int]:
        """Return the functions that refer to a name in a module, by name or as an
        attribute, from where they see that module."""
        return {
            index
            for index in self.global_mentions[name] | self.attribute_mentions[name]
            if self.sees(self.function_files[index], path, index)
        }

    def keeps_attribute(
        self,
        path: str,
        cls: str,
        name: str,
        statements: Callable[[str], list[names.Statement]],
    ) -> bool:
        """Return whether only code that looks an attribute up can see it change.

        The class and its subclasses must be plain, and no top-level statement
        that refers to one of them, such as a subclass or `X = Model.name`, may
        mention the name. `statements` returns a recorded module's statements.
        """
        classes = [(path, cls), *self.descendants[path, cls]]
        return self.plain.issuperset(classes) and not any(
            name in statement.attributes | statement.names
            for _, class_name in classes
            for reference_path, kind, defined, *_ in self.references.get(class_name, [])
            if kind != "import"
            for statement in statements(reference_path)
            if statement.defined & set(defined)
        )

    def calls(self, path: str, cls: str, method: str) -> set[int] | None:
        """Return the keys of Django's calls of a method a class overrides.

        That is, when the class or a subclass inherits it from Django, which records
        each call, so only those calls find the override. Otherwise it is None.
        """
        classes = [(path, cls), *self.descendants[path, cls]]
        inheriting = [key for key in classes if method in self.inherited.get(key, ())]
        if not inheriting or not all(method in self.tracked[key] for key in inheriting):
            return None
        return {
            index
            for key_path, name in classes
            if (
                index := self.function_index.get((key_path, f"call:{name}.{method}", 0))
            )
            is not None
        }

    def overrides(self, path: str, cls: str, method: str) -> bool:
        """Return whether code the recording can't see may look a class member up.

        It may when the class or a subclass inherits that name from outside the
        project, when the name is special or one frameworks build, when the class
        is a protocol, whose members isinstance() checks, or when the recording
        does not know the class.
        """
        key = (path, cls)
        return (
            key not in self.inherited
            or "_is_protocol" in self.inherited[key]
            or (method.startswith("__") and method.endswith("__"))
            or method in DISPATCHED
            or method.startswith(DISPATCHED_PREFIXES)
            or any(
                method in self.inherited.get(other, ())
                for other in [key, *self.descendants[key]]
            )
        )

    def follow_import_time(self, affected: Affected, old_source: Source) -> None:
        """Add the modules whose import ran functions a change reaches.

        Functions a changed name reaches may have run at import time, or outside
        tests, as a directly changed one may. Class bodies, whose changes follow
        their statements, and class and framework keys, only say what used a class
        or module. Code compiled from another language, as Jinja compiles a
        template to Python under the template's path, counts as a function.
        """

        @functools.cache
        def functions(path: str) -> set[tuple[str, int]] | None:
            # Code that ran compiled, so its source does.
            try:
                found = parse_functions(old_source(path) or "", path)[0]
            except (SyntaxError, ValueError):
                return None
            return {(function.qualname, function.first_line) for function in found}

        for index in affected.readers:
            file = self.function_files[index]
            qualname = self.function_qualnames[index]
            known = functions(file)
            if (
                known is not None
                and (qualname, self.function_lines[index]) not in known
            ):
                continue
            if index in self.session_functions:
                msg = f"{qualname} in {file} runs outside tests"
                raise RunAllError(msg)
            affected.modules |= self.import_functions[index]

    def follow_names(self, affected: Affected) -> None:
        """Add what uses changed top-level names, following definitions that use them.

        A test uses a name when it ran a function that mentions it, or when the name
        is a class, ran its methods, ran a method for one of its instances, or used
        it through a Django hook. A plain name means the binding in the same module,
        which other modules get by importing it; an attribute or string may mean the
        name in any module.

        What a top-level statement computes from a changed value changes too, and a
        statement doing more than binding names, such as registering something,
        changes its module. A class or function changes only what is built from it:
        subclasses, classes that assign it, as in `Meta.model`, and values read
        from its attributes. What merely holds, passes or calls it at import time,
        such as a registry, a ForeignKey or `View.as_view()`, sees the change
        through the class or function itself.
        """
        changed = set(affected.names)
        pending = list(changed)
        seen: set[tuple[str, str, str]] = set()
        while pending:
            path, name, kind = pending.pop()
            if (path, name, kind) in seen:
                continue
            seen.add((path, name, kind))
            affected.top.add(path)
            # Django reads a URLconf's patterns, which may hold a value changed
            # elsewhere, as in `include(extra_patterns)`.
            if (
                kind == "value"
                and path in self.url_modules
                and (path, name, kind) not in affected.names
            ):
                affected.urls.append((path, None, ""))
            # A string may name it, as `getattr(conf, "VALUE")` or a namespace's
            # `["VALUE"]` after runpy, from code that need not import its module.
            # A name an import binds is only looked up through the module, as
            # code reading its own values from a namespace wouldn't.
            readers = (
                {
                    index
                    for index in self.attribute_mentions[name]
                    if self.sees(self.function_files[index], path, index)
                }
                | {
                    index
                    for index in self.global_mentions[name]
                    if self.function_files[index] == path
                }
                | {
                    index
                    for index in self.string_mentions[name]
                    if kind != "binding"
                    or self.sees(self.function_files[index], path, index)
                }
            )
            affected.functions |= self.members[path, name] | readers
            if kind != "definition":
                affected.readers |= readers
            # Methods that ran as modules loaded, as `VALUE = Child.build()` runs an
            # inherited classmethod, or `__init_subclass__` runs as a subclass is made,
            # may have read a changed class's attributes.
            elif (path, name) in self.inherited:
                # Instances held elsewhere, as `x = make()` in a class body, change
                # without their methods running when a special method comes or goes,
                # or when library code they inherit from calls or reads them, as
                # Django does with a form's field, unless a hook credits the class
                # wherever tests use it.
                if (path, name) in affected.special or (
                    (path, name) not in self.plain and name not in self.covered
                ):
                    affected.functions |= self.class_users(path, name)
                # Only for a class the change made, with its ancestors and subclasses:
                # a class merely built from it, as a manager or form naming a model,
                # runs methods as modules load that don't read its attributes.
                if (path, name, kind) in changed:
                    affected.readers |= {
                        index
                        for key in [
                            (path, name),
                            *self.ancestors[path, name],
                            *self.descendants[path, name],
                        ]
                        for index in self.members[key]
                        if index in self.import_functions
                        or index in self.session_functions
                    }
            # A value the settings module imports, as with `from .base import *`.
            if path == self.settings_file:
                self.setting_change(path, name, affected)
            # Pytest reads these itself, as a skip mark in `pytestmark`.
            if (
                path.rpartition("/")[2] == "conftest.py" or path in self.test_files
            ) and (
                name == "pytestmark" or name.startswith(("pytest_", "collect_ignore"))
            ):
                affected.modules.add(path)
                affected.reasons.setdefault(path, f"pytest reads {name} in {path}")
            pending.extend(self.dependents(path, name, kind, affected))

    def dependents(
        self, path: str, name: str, kind: str, affected: Affected
    ) -> list[tuple[str, str, str]]:
        """Return the names that change with a changed name."""
        module = self.module_names.get(path)
        result = []
        for entry in self.references.get(name, []):
            reference_path, reference_kind, defined, how, modules, derives = entry
            # An imported object is the same object in the module it came from, so
            # setting its items or attributes changes it there, and a name a header
            # uses is used there.
            if (
                kind in {"value", "used"}
                and reference_kind == "import"
                and reference_path == path
                and name in defined
            ):
                result += [
                    (self.module_paths[source], name, kind)
                    for source in modules
                    if source in self.module_paths
                ]
            if (
                kind == "used"
                or (how == "name" and reference_path != path)
                or (
                    how == "attribute"
                    and (kind == "binding" or not self.sees(reference_path, path))
                )
                or (how == "import" and module not in modules)
                or (kind == "definition" and reference_kind != "import" and not derives)
            ):
                continue
            if reference_kind == "other":
                if name not in self.covered:
                    affected.modules.add(reference_path)
                    affected.reasons.setdefault(
                        reference_path,
                        f"{reference_path} uses {name} from {path} at import time",
                    )
                continue
            next_kind = {"class": "definition", "def": "definition"}.get(
                reference_kind, "value" if reference_kind == "assign" else kind
            )
            result += [(reference_path, other, next_kind) for other in defined]
        result += [
            (star_path, name, kind)
            for star_path, star_modules in self.stars.items()
            if module in star_modules
        ]
        return result

    def sees(self, path: str, other: str, index: int | None = None) -> bool:
        """Return whether a module, or one of its functions, can reach another
        module's names as attributes.

        It can if it is that module or imports it, or it or a module it imports
        holds it, at any depth, as `from pkg import module`, `import pkg.module` or
        a loader returning a module does. A function also reaches a package's
        submodule, as `pkg.sub.VALUE`, when it names each module on the way.
        Modules recorded without their imports, such as unloaded ones, can reach
        anything.
        """
        if path not in self.imported or path == other or other in self.imported[path]:
            return True
        held = self.holding(path)
        if other in held:
            return True
        parts = self.module_names.get(other, "").split(".")
        return index is not None and any(
            self.module_paths.get(".".join(parts[:end])) in held
            and all(index in self.attribute_mentions[part] for part in parts[end:])
            for end in range(1, len(parts))
        )

    def holding(self, path: str) -> set[str]:
        """Return the modules a module or those it imports hold, at any depth."""
        held = self.held.get(path)
        if held is None:
            held = self.held[path] = set()
            pending = {
                module
                for importer in {path, *self.imported.get(path, ())}
                for module in self.holds.get(importer, ())
            }
            while pending:
                held |= pending
                pending = {
                    other for module in pending for other in self.holds.get(module, ())
                } - held
        return held

    def unimported(self, path: str) -> bool:
        """Return whether something other than project code or pytest's collection,
        as of a test file that skips itself and so has no tests, loaded a module."""
        return (
            path in self.loaded
            and not self.importers[path]
            and path not in self.test_files
            and not any(
                fnmatchcase(path.rpartition("/")[2], pattern)
                for pattern in self.python_files
            )
        )

    def defines_only(self, path: str, source: str | None) -> bool:
        """Return whether a module's code ran only for tests, as fixtures and
        functions tests call do, not as hooks running around them do, and its top
        level only defines names and plain classes, without calling anything or
        registering anything that might change what other tests see."""
        if any(self.function_files[index] == path for index in self.session_functions):
            return False
        try:
            tree = ast.parse(source or "")
        except (SyntaxError, ValueError):
            return False
        return all(
            isinstance(
                node,
                ast.Import | ast.ImportFrom | ast.FunctionDef | ast.AsyncFunctionDef,
            )
            or (
                isinstance(node, ast.ClassDef)
                and not node.decorator_list
                and (path, node.name) in self.plain
            )
            or (
                isinstance(node, ast.Assign | ast.AnnAssign | ast.If | ast.Expr)
                and not any(isinstance(child, ast.Call) for child in ast.walk(node))
            )
            for node in tree.body
        )

    def closure(
        self,
        modules: set[str],
        reasons: dict[str, str],
        new_source: Source | None = None,
    ) -> set[str]:
        """Return modules whose top level changed and every module importing them.

        A framework loads modules that no project module imports, and tests reach
        what they import without touching it, so reaching one of them could affect
        any test. That is, unless the recording credits tests with the module where
        the framework uses it, as with a URLconf, and never outside tests, or the
        module is a template tag library, which reaches tests through the templates
        that load it, or only defines names, as defines_only says, which reach tests
        that use them; a `conftest.py` that only defines names reaches the test
        files below it through its fixtures.
        """
        affected: set[str] = set()
        loading: set[str] = set()
        for start in modules:
            change = reasons.get(start, f"top-level code in {start} changed")
            pending = [start]
            while pending:
                path = pending.pop()
                if path in affected:
                    continue
                affected.add(path)
                used_in_tests = (
                    path in self.framework_modules
                    and self.session_functions.isdisjoint(self.framework_modules[path])
                ) or (path in self.libraries or path in self.builtins)
                defines_only = new_source is not None and self.defines_only(
                    path, new_source(path)
                )
                if defines_only and path.rpartition("/")[2] == "conftest.py":
                    directory = path.removesuffix("conftest.py")
                    pending.extend(
                        test_file
                        for test_file in self.test_files
                        if test_file.startswith(directory)
                    )
                elif path in self.loaders:
                    # Code that loaded it by name sees the change where it runs, not
                    # in what imports that code's module.
                    loading.update(self.loaders[path])
                # A module that only defines names reaches tests through them,
                # however it was loaded, as Sphinx loads extensions a project's
                # conf.py names.
                elif self.unimported(path) and not used_in_tests and not defines_only:
                    msg = f"{change} and {path} imports it"
                    if path == start:
                        msg = f"{change} and nothing imports it"
                    raise RunAllError(msg)
                pending.extend(self.importers[path])
        return affected | loading
