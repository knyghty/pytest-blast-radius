"""Work out which top-level names a change rebinds, and what refers to them."""

import ast
import sys
from collections import Counter
from collections.abc import Collection
from collections.abc import Iterable
from dataclasses import dataclass

# pytest marks, which only describe the test they decorate.
MARKS = ("pytest.mark.", "mark.")
# Decorators that wrap or describe a definition without registering it anywhere.
PURE_DECORATORS = {
    "abstractmethod",
    "asynccontextmanager",
    "cache",
    "cached_property",
    "classmethod",
    "contextmanager",
    "dataclass",
    "deleter",
    "fixture",
    "final",
    "getter",
    "lru_cache",
    "overload",
    "override",
    "property",
    "runtime_checkable",
    "setter",
    "singledispatch",
    "staticmethod",
    "total_ordering",
    "wraps",
}


@dataclass(frozen=True)
class Statement:
    """A top-level statement, the names it binds and the names it refers to.

    `kind` is "def", "class", "assign", "import" or "star" for statements that only
    bind names, "skip" for `if TYPE_CHECKING:`, or "other" for statements that do
    more, such as a call registering something. `names` are plain names, which
    resolve in this module, and `attributes` are attribute names and the parts of
    strings, which may belong to any module. `derives` are the names the statement
    builds on rather than merely holds or calls: names whose attributes it reads,
    as in `Model.Status.choices`, names it aliases, and a class's bases and the
    names its body assigns, as in `Meta.model = Product`. `header` holds a
    definition's decorator and class header lines, and `decorators` the callables
    decorating it. `members` are a class body's statements: their kind, the names
    they bind, and their first and last lines. The kind is "method" for a method
    whose decorators are all pure, "attribute" for an assignment to plain names,
    or "other". `loads` are the names a class body reads as it runs.
    """

    start: int
    end: int
    kind: str
    defined: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    attributes: frozenset[str] = frozenset()
    derives: frozenset[str] = frozenset()
    header: frozenset[int] = frozenset()
    header_names: frozenset[str] = frozenset()
    decorators: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    members: tuple[tuple[str, tuple[str, ...], int, int], ...] = ()
    loads: frozenset[str] = frozenset()
    # The lines of decorators that may register the definition, as `@receiver(...)`.
    registering: frozenset[int] = frozenset()


class _Names(ast.NodeVisitor):
    """Collect the names code refers to, skipping function bodies, which run later."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.attributes: set[str] = set()
        self.reads: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._chain(node, called=False)

    def visit_Call(self, node: ast.Call) -> None:
        self._chain(node.func, called=True)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            self.visit(argument)

    def _chain(self, node: ast.expr, *, called: bool) -> None:
        """Collect a dotted chain; reading `a.b.c` without calling it reads a and b."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        self.attributes.update(parts)
        if isinstance(node, ast.Name):
            self.names.add(node.id)
            parts.append(node.id)
        else:
            self.visit(node)
        if not called:
            self.reads.update(parts[1:])

    def visit_Constant(self, node: ast.Constant) -> None:
        # Django refers to models by strings such as "app.Model".
        if isinstance(node.value, str):
            self.attributes.update(
                part for part in node.value.split(".") if part.isidentifier()
            )

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Annotations count too: code such as typing.get_type_hints() reads them.
        arguments = node.args
        for child in [
            *node.decorator_list,
            *arguments.defaults,
            *arguments.kw_defaults,
            *(
                argument.annotation
                for argument in [
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                    *filter(None, [arguments.vararg, arguments.kwarg]),
                ]
            ),
            node.returns,
        ]:
            if child is not None:
                self.visit(child)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_all(self, nodes: list[ast.expr]) -> "_Names":
        for node in nodes:
            self.visit(node)
        return self


def _references(node: ast.AST) -> _Names:
    visitor = _Names()
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        visitor.visit_FunctionDef(node)
    else:
        visitor.visit(node)
    return visitor


def _is_type_checking(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _definition(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> Statement:
    start, end = node.lineno, node.end_lineno or node.lineno
    first = min([start, *(decorator.lineno for decorator in node.decorator_list)])
    header = set(range(first, start))
    # Pure decorators and marks only describe the definition, so what their
    # arguments name stays as it was.
    header_nodes: list[ast.expr] = [
        decorator
        for decorator in node.decorator_list
        if not _describes(decorator_name(decorator))
    ]
    if isinstance(node, ast.ClassDef):
        header |= set(range(start, node.body[0].lineno))
        header_nodes += [*node.bases, *(keyword.value for keyword in node.keywords)]
    elif header_nodes:
        # A decorator gets the whole function, and may read its signature, as
        # singledispatch's register reads its annotations.
        header |= set(range(start, node.body[0].lineno))
    references = _references(node)
    head = _Names().visit_all(header_nodes)
    defined = frozenset([node.name])
    derives = set(references.reads)
    if isinstance(node, ast.ClassDef):
        bases = _Names().visit_all(node.bases)
        derives |= bases.names | bases.attributes | _class_assignments(node)
    return Statement(
        first,
        end,
        "class" if isinstance(node, ast.ClassDef) else "def",
        defined,
        frozenset(references.names) - defined,
        frozenset(references.attributes),
        frozenset(derives),
        frozenset(header),
        frozenset(head.names | head.attributes),
        tuple(decorator_name(decorator) for decorator in node.decorator_list),
        # Blocks that never run hold no members, so changing them changes nothing.
        members=tuple(
            _member(child) for child in node.body if not _is_type_checking(child)
        )
        if isinstance(node, ast.ClassDef)
        else (),
        loads=_class_loads(node) if isinstance(node, ast.ClassDef) else frozenset(),
        registering=frozenset(
            line
            for decorator in node.decorator_list
            if not _describes(decorator_name(decorator))
            for line in range(decorator.lineno, (decorator.end_lineno or 0) + 1)
        ),
    )


def _class_loads(node: ast.ClassDef) -> frozenset[str]:
    """Return the names a class body reads as it runs, which skips method bodies."""
    loads = set()
    pending: list[ast.AST] = list(node.body)
    while pending:
        child = pending.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            pending += [
                *child.decorator_list,
                *child.args.defaults,
                *(default for default in child.args.kw_defaults if default),
            ]
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            loads.add(child.id)
        else:
            pending.extend(ast.iter_child_nodes(child))
    return frozenset(loads)


def _member(node: ast.stmt) -> tuple[str, tuple[str, ...], int, int]:
    end = node.end_lineno or node.lineno
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        first = min(
            [node.lineno, *(decorator.lineno for decorator in node.decorator_list)]
        )
        # ABCMeta collects abstract methods as it makes the class.
        pure = all(
            decorator_name(decorator).rpartition(".")[2]
            in PURE_DECORATORS - {"abstractmethod"}
            for decorator in node.decorator_list
        )
        return "method" if pure else "other", (node.name,), first, end
    targets = (
        node.targets
        if isinstance(node, ast.Assign)
        else [node.target]
        if isinstance(node, ast.AnnAssign)
        else []
    )
    if targets and all(isinstance(target, ast.Name) for target in targets):
        names = tuple(target.id for target in targets if isinstance(target, ast.Name))
        return "attribute", names, node.lineno, end
    return "other", (), node.lineno, end


def _describes(decorator: str) -> bool:
    return decorator.rpartition(".")[2] in PURE_DECORATORS or decorator.startswith(
        MARKS
    )


def decorator_name(node: ast.expr) -> str:
    """Return a decorator's callable, marking autouse fixtures, which run everywhere."""
    if not isinstance(node, ast.Call):
        return ast.unparse(node)
    autouse = any(
        keyword.arg == "autouse"
        and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
        for keyword in node.keywords
    )
    return ast.unparse(node.func) + (" autouse" if autouse else "")


def _dotted(node: ast.expr | None) -> set[str]:
    """Return the parts of a plain dotted name such as `models.Product`, or none."""
    parts = set()
    while isinstance(node, ast.Attribute):
        parts.add(node.attr)
        node = node.value
    return parts | {node.id} if isinstance(node, ast.Name) else set()


def _class_assignments(node: ast.ClassDef) -> set[str]:
    """Return the names a class body, or a nested class's, sets attributes to.

    Frameworks build classes from these, as a ModelForm builds its fields from
    `Meta.model`.
    """
    names = set()
    for child in node.body:
        if isinstance(child, ast.Assign | ast.AnnAssign):
            names |= _dotted(child.value)
        elif isinstance(child, ast.ClassDef):
            names |= _class_assignments(child)
    return names


def _import(node: ast.Import | ast.ImportFrom, package: str) -> Statement:
    start, end = node.lineno, node.end_lineno or node.lineno
    modules = tuple(imported_modules(node, package))
    if any(alias.name == "*" for alias in node.names):
        return Statement(start, end, "star", modules=modules)
    defined = frozenset(
        alias.asname or alias.name.partition(".")[0]
        if isinstance(node, ast.Import)
        else alias.asname or alias.name
        for alias in node.names
    )
    # An import refers to the names it binds, as other modules define them.
    imported = frozenset(alias.name.rpartition(".")[2] for alias in node.names)
    return Statement(start, end, "import", defined, imported, modules=modules)


def statement(
    node: ast.stmt, package: str, imported: Collection[str] = ()
) -> Statement:
    """Return what a top-level statement binds and refers to.

    `imported` are the names the module's imports bind. Setting an item or
    attribute of one changes another module's object, and a walrus binds names
    the statement's targets don't show, so either does more than bind names.
    """
    start, end = node.lineno, node.end_lineno or node.lineno
    references = _references(node)
    walrus = _walrus(node)
    if not walrus and isinstance(
        node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ):
        return _definition(node)
    if _is_type_checking(node):
        return Statement(start, end, "skip")
    if isinstance(node, ast.Import | ast.ImportFrom):
        return _import(node, package)
    if not walrus and isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        defined = assigned_names(targets)
        if defined is not None and not any(
            _root(part) in imported
            for target in targets
            for part in ast.walk(target)
            if isinstance(part, ast.Attribute | ast.Subscript)
        ):
            return Statement(
                start,
                end,
                "assign",
                defined,
                frozenset(references.names) - defined,
                frozenset(references.attributes),
                frozenset(references.reads | _dotted(node.value)),
            )
    if (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ):
        return Statement(start, end, "assign", frozenset(["__doc__"]))
    return Statement(
        start,
        end,
        "other",
        names=frozenset(references.names),
        attributes=frozenset(references.attributes),
        derives=frozenset(references.reads),
    )


def _root(node: ast.expr) -> str | None:
    """Return the name an attribute or item chain starts from, as `a` in `a.b[0]`."""
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _walrus(node: ast.stmt) -> bool:
    """Return whether a top-level statement binds a name with `:=` as it runs,
    leaving out function bodies, which run later."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        arguments = node.args
        parts: list[ast.AST] = [
            *node.decorator_list,
            *arguments.defaults,
            *filter(None, arguments.kw_defaults),
        ]
    elif isinstance(node, ast.ClassDef):
        parts = [*node.decorator_list, *node.bases, *node.keywords]
    else:
        parts = [node]
    return any(
        isinstance(child, ast.NamedExpr) for part in parts for child in ast.walk(part)
    )


def assigned_names(targets: list[ast.expr]) -> frozenset[str] | None:
    """Return the names assignment targets bind or change, or None for others.

    Setting an item or attribute of a name changes what the name refers to.
    """
    names = set()
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Tuple | ast.List):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        elif isinstance(target, ast.Attribute | ast.Subscript):
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            pending.append(target.value)
        elif isinstance(target, ast.Name):
            names.add(target.id)
        else:
            return None
    return frozenset(names)


def imported_modules(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """Return the dotted names an import statement may load."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if node.level:
        parts = package.split(".")
        base = ".".join(parts[: len(parts) - node.level + 1])
        module = f"{base}.{module}" if module else base
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def bindings(tree: ast.Module, package: str) -> dict[str, str | None]:
    """Return what each name a module binds refers to: the dotted name it imports,
    or None for its own definitions."""
    bound: dict[str, str | None] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    first = alias.name.partition(".")[0]
                    bound[first] = first
                    bound[alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = imported_modules(node, package)[0]
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{module}.{alias.name}"
        else:
            bound |= dict.fromkeys(statement(node, package).defined)
    return bound


def resolve(dotted: str, bound: dict[str, str | None]) -> str | None:
    """Return the dotted name a module's dotted name refers to.

    It is None when the module doesn't bind it, and empty for the module's own
    definitions.
    """
    prefix = max(
        (name for name in bound if dotted == name or dotted.startswith(f"{name}.")),
        key=len,
        default=None,
    )
    if prefix is None:
        return None
    target = bound[prefix]
    return "" if target is None else target + dotted[len(prefix) :]


def needs(source: str, package: str, *, own: bool, module: str, name: str) -> bool:
    """Return whether a module's top level needs a module's name to load: imports
    it, reads it as an attribute, as in `models.Product` after
    `from shop import models`, or, in its own module or after importing everything
    from the module, uses it.

    Function bodies don't run on import, so they don't count; anything else,
    such as a class body, a decorator or an import inside `if` or `try`, does.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True
    bound = bindings(tree, package)
    pending: list[ast.AST] = list(tree.body)
    star = uses = False
    # Before Python 3.14, a def evaluates its annotations.
    eager = sys.version_info < (3, 14) and not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            pending += [
                *getattr(node, "decorator_list", []),
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default),
            ]
            if eager and not isinstance(node, ast.Lambda):
                arguments = node.args
                pending += [
                    argument.annotation
                    for argument in [
                        *arguments.posonlyargs,
                        *arguments.args,
                        *arguments.kwonlyargs,
                        *filter(None, [arguments.vararg, arguments.kwarg]),
                    ]
                    if argument.annotation
                ]
                pending += [node.returns] if node.returns else []
            continue
        if (
            isinstance(node, ast.ImportFrom)
            and imported_modules(node, package)[0] == module
        ):
            if any(alias.name == name for alias in node.names):
                return True
            star = star or any(alias.name == "*" for alias in node.names)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and (dotted := _dotted_name(node.value)) is not None
            and resolve(dotted, bound) == module
        ):
            return True
        uses = uses or (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
        )
        pending.extend(ast.iter_child_nodes(node))
    return uses and (own or star)


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (value := _dotted_name(node.value)):
        return f"{value}.{node.attr}"
    return None


def module_statements(tree: ast.Module, package: str) -> list[Statement]:
    """Return a parsed module's top-level statements."""
    imported = {
        name
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _import(node, package).defined
    }
    return [statement(node, package, imported) for node in tree.body]


def parse_statements(source: str, package: str = "") -> list[Statement]:
    """Return a module's top-level statements."""
    return module_statements(ast.parse(source), package)


@dataclass(frozen=True)
class Changes:
    """The top-level names a change rebinds.

    Code computed from a changed value at import time changes with it. A class or
    function changes where it is used and in what is built from it, such as its
    subclasses, but not in what merely holds it, such as a registry. `bindings` are
    names an import now binds differently, which changes nothing where they came
    from. `used` are the names a changed decorator or class header refers to.
    `members` are the class members a change adds, changes or removes, where it
    touches nothing else in the class: the class, the member's name, and "method"
    for an added method or "attribute" for an assigned name. `removed` are the
    names the module no longer binds, which may break what imports them.
    """

    values: frozenset[str] = frozenset()
    definitions: frozenset[str] = frozenset()
    used: frozenset[str] = frozenset()
    bindings: frozenset[str] = frozenset()
    members: frozenset[tuple[str, str, str]] = frozenset()
    removed: frozenset[str] = frozenset()


def registers(
    old: Statement | None, new: Statement | None, registries: Iterable[str] = ()
) -> bool:
    """Return whether a definition gained a decorator that may register it.

    Registering it in one of `registries` doesn't count.
    """
    added = Counter(new.decorators if new else ()) - Counter(
        old.decorators if old else ()
    )
    # Marks and fixtures only affect tests that use them, apart from autouse ones.
    local = tuple(f"{registry}." for registry in registries)
    return any(
        not _describes(decorator) and not decorator.startswith(local)
        for decorator in added
    )


def libraries(source: str) -> frozenset[str]:
    """Return the names a module binds to template libraries, as `register` in
    `register = template.Library()`."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return frozenset()
    return frozenset(
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func).rpartition(".")[2] == "Library"
        for target in node.targets
        if isinstance(target, ast.Name)
    )


def changed_names(  # noqa: PLR0913
    old: str,
    new: str,
    package: str,
    lines: tuple[set[int], set[int]],
    loaded: set[str],
    *,
    registries: Iterable[str] = (),
    registering: bool = True,
) -> Changes | None:
    """Return the top-level names a change rebinds, or None if it does more.

    `lines` are the changed top-level lines on the old and new side. A change does
    more when it touches a statement that is not a plain binding, gives a definition
    a decorator that may register it, other than in one of `registries`, changes
    such a decorator's arguments when `registering`, or imports a module nothing
    loaded before.
    """
    try:
        sides = [parse_statements(source, package) for source in (old, new)]
    except (SyntaxError, ValueError):
        return None
    definitions = [
        {name: statement for statement in statements for name in statement.defined}
        for statements in sides
    ]
    values: set[str] = set()
    defined: set[str] = set()
    used: set[str] = set()
    bindings: set[str] = set()
    members: set[tuple[str, str, str]] = set()
    for side, (statements, changed) in enumerate(zip(sides, lines, strict=True)):
        for statement in statements:
            touched = {
                line for line in changed if statement.start <= line <= statement.end
            }
            if not touched or statement.kind == "skip":
                continue
            if (
                statement.kind in {"other", "star"}
                or (
                    statement.kind == "import"
                    and not any(module in loaded for module in statement.modules)
                )
                or (
                    registering
                    and touched & statement.registering
                    and registers(None, statement, registries)
                )
            ):
                return None
            for name in statement.defined:
                if statement.kind in {"def", "class"} and registers(
                    definitions[0].get(name), definitions[1].get(name), registries
                ):
                    return None
            touched_members = changed_members(
                statement, definitions[1 - side], touched, adding=side == 1
            )
            if touched_members is not None:
                members |= touched_members
            elif statement.kind in {"def", "class"}:
                defined |= statement.defined
            elif statement.kind == "import":
                bindings |= statement.defined
            else:
                values |= statement.defined
            if touched & statement.header:
                used |= statement.header_names
    changes = Changes(
        *map(frozenset, (values, defined, used, bindings)),
        frozenset(member for member in members if member[0] not in defined),
        frozenset(definitions[0].keys() - definitions[1].keys()),
    )
    return (
        None
        if registering and _registering_reads(sides[1], changes, registries)
        else changes
    )


def _registering_reads(
    statements: list[Statement], changes: Changes, registries: Iterable[str]
) -> bool:
    """Return whether a decorator that may register reads a changed value as the
    module loads, as `@pytest.fixture(autouse=ENABLED)` does."""
    return any(
        statement.header_names & (changes.values | changes.bindings)
        and registers(None, statement, registries)
        for statement in statements
    )


def changed_members(
    statement: Statement,
    others: dict[str, Statement],
    touched: set[int],
    *,
    adding: bool,
) -> set[tuple[str, str, str]] | None:
    """Return the class members a change touches, or None if it does more.

    Each member is the class, its name and "method" or "attribute". `others` are
    the other side's definitions. Changed lines may be blank lines, comments,
    assignments to plain names and, on the new side, methods the old side lacks,
    whose decorators are all pure. Assignments in a decorated class count as more,
    since decorators such as `dataclass` build code from them, as do assignments
    to names the class body reads, which change what it computes.
    """
    if statement.kind != "class":
        return None
    [cls] = statement.defined
    other = others.get(cls)
    if other is None or other.kind != "class" or touched & statement.header:
        return None
    existing = {name for member in other.members for name in member[1]}
    changed = set()
    for line in touched:
        for kind, names, first, last in statement.members:
            if not first <= line <= last:
                continue
            if kind == "other" or (
                kind == "method" and (not adding or names[0] in existing)
            ):
                return None
            changed |= {(cls, name, kind) for name in names}
    attributes = {name for _, name, kind in changed if kind == "attribute"}
    if attributes and (
        statement.decorators or attributes & (statement.loads | other.loads)
    ):
        return None
    return changed
