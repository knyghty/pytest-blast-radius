"""Work out which requested paths and URL names a change to a URLconf can reach."""

import ast
import builtins
import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

from pytest_blast_radius import names

# Django's path converters; any other may match anything.
CONVERTERS = {
    "int": "[0-9]+",
    "path": ".+",
    "slug": "[-a-zA-Z0-9_]+",
    "str": "[^/]+",
    "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
}
PARAMETER = re.compile(r"<(?:(?P<converter>[^>:]+):)?[^>]+>")


@dataclass
class Change:
    """What changed URL patterns can reach.

    `paths` match the requested paths a changed pattern could serve, `names` are
    the changed patterns' names, and `includes` says whether an include changed,
    which moves every name under it and may change namespaces. `uses` are the
    dotted names new patterns and imports refer to, and `modules` the modules
    new includes name, which must exist for the URLconf to load.
    """

    paths: list[re.Pattern[str]] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    includes: bool = False
    uses: set[str] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)


def route_pattern(route: str, *, regex: bool, endpoint: bool) -> re.Pattern[str]:
    """Return a pattern that finds the paths a route serves, as its URLconf sees them.

    A route starts the path, and an endpoint's ends it too. A regex matches as
    Django searches with it.
    """
    if regex:
        return re.compile(route)
    parts = []
    position = 0
    for match in PARAMETER.finditer(route):
        parts.append(re.escape(route[position : match.start()]))
        parts.append(CONVERTERS.get(match["converter"] or "str", ".+"))
        position = match.end()
    parts.append(re.escape(route[position:]))
    return re.compile("^" + "".join(parts) + (r"\Z" if endpoint else ""))


def _entries(node: ast.stmt) -> list[ast.expr] | None:
    """Return the patterns a statement assigns, or None if it does anything else.

    The list a statement extends, as in `urlpatterns = urlpatterns + [...]`,
    adds nothing; another list it refers to is an entry, which only an unchanged
    statement may hold.
    """
    if not isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
        return None
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    extended = {ast.unparse(target) for target in targets}
    values = [node.value]
    elements = []
    while values:
        value = values.pop()
        if isinstance(value, ast.List | ast.Tuple):
            elements += value.elts
        elif isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            values += [value.left, value.right]
        elif isinstance(value, ast.Name):
            if value.id not in extended:
                elements.append(value)
        else:
            return None
    return elements


def _last(node: ast.expr) -> str:
    return ast.unparse(node).rpartition(".")[2]


def _entry(node: ast.expr, change: Change, held: set[str]) -> bool:
    """Add what a `path()` or `re_path()` entry reaches; False for anything else.

    `held` are the names the URLconf assigns, which may hold an include.
    """
    if not (
        isinstance(node, ast.Call)
        and _last(node.func) in {"path", "re_path"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return False
    names = [keyword.value for keyword in node.keywords if keyword.arg == "name"]
    for name in [*names, *node.args[3:4]]:
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            return False
        change.names.add(name.value)
    # A view is a function, a method or a class's as_view(); anything else, such
    # as include() or admin.site.urls, may hold more patterns.
    target = node.args[1] if len(node.args) > 1 else None
    view = (isinstance(target, ast.Name) and target.id not in held) or (
        isinstance(target, ast.Attribute) and _last(target) != "urls"
    )
    view |= isinstance(target, ast.Call) and _last(target.func) == "as_view"
    change.includes |= not view
    change.paths.append(
        route_pattern(
            node.args[0].value, regex=_last(node.func) == "re_path", endpoint=view
        )
    )
    return True


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (value := _dotted(node.value)):
        return f"{value}.{node.attr}"
    return None


def _uses(node: ast.expr, change: Change) -> None:
    """Add the dotted names and included modules an entry refers to."""
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if (dotted := _dotted(current)) is not None:
            change.uses.add(dotted)
            continue
        if (
            isinstance(current, ast.Call)
            and _last(current.func) == "include"
            and current.args
            and isinstance(current.args[0], ast.Constant)
            and isinstance(current.args[0].value, str)
        ):
            change.modules.add(current.args[0].value)
        pending.extend(ast.iter_child_nodes(current))


def _bound(node: ast.Import | ast.ImportFrom) -> set[str]:
    """Return what an import binds, with the whole path of a plain `import a.b`."""
    return {alias.asname or alias.name for alias in node.names}


def changes(old: str, new: str) -> Change | None:
    """Return what a change to a URLconf reaches, or None if it does more.

    Every changed top-level statement must be an import or assign lists of
    `path()` and `re_path()` entries with a literal route and name. A changed
    entry may serve the paths its old and new routes match, and reverses its old
    and new names.
    """
    try:
        sides = [ast.parse(source).body for source in (old, new)]
    except (SyntaxError, ValueError):
        return None
    texts = [[ast.unparse(node) for node in body] for body in sides]
    held = {
        target.id
        for body in sides
        for node in body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    change = Change()
    matcher = difflib.SequenceMatcher(a=texts[0], b=texts[1], autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        # A new import must load, and what an old one bound may still be used.
        for node in [*sides[0][old_start:old_end], *sides[1][new_start:new_end]]:
            if isinstance(node, ast.Import | ast.ImportFrom):
                change.uses |= _bound(node)
        statements = [
            [] if isinstance(node, ast.Import | ast.ImportFrom) else _entries(node)
            for node in [
                *sides[0][old_start:old_end],
                *sides[1][new_start:new_end],
            ]
        ]
        if any(entries is None for entries in statements):
            return None
        nodes = {
            ast.unparse(entry): entry
            for entries in statements
            for entry in entries or []
        }
        old_entries, new_entries = (
            [ast.unparse(entry) for entries in side for entry in entries or []]
            for side in (
                statements[: old_end - old_start],
                statements[old_end - old_start :],
            )
        )
        changed = set(old_entries).symmetric_difference(new_entries)
        # Patterns match in order, so reordering the others changes them too.
        common = set(old_entries) & set(new_entries)
        if [entry for entry in old_entries if entry in common] != [
            entry for entry in new_entries if entry in common
        ]:
            changed |= common
        if not all(_entry(nodes[text], change, held) for text in changed):
            return None
        for text in changed & set(new_entries):
            entry = nodes[text]
            for argument in [
                *getattr(entry, "args", [])[1:],
                *(keyword.value for keyword in getattr(entry, "keywords", [])),
            ]:
                _uses(argument, change)
    return change


def unresolved(
    source: str, package: str, change: Change, find: Callable[[str], str | None]
) -> bool:
    """Return whether a changed URLconf may fail to load.

    It may when a name a new pattern or import uses isn't bound in the module or
    built in, or names a project module or attribute that doesn't exist. Modules
    outside the project, which `find` doesn't know, are trusted, as are
    attributes beyond a project module's own names.
    """
    bound = names.bindings(ast.parse(source), package)
    for use in change.uses:
        if (target := names.resolve(use, bound)) is None:
            if use.partition(".")[0] not in vars(builtins):
                return True
        elif target and _missing(target, find):
            return True
    return any(_missing(module, find) for module in change.modules)


def _missing(dotted: str, find: Callable[[str], str | None]) -> bool:
    """Return whether a dotted name names a project module or attribute that
    doesn't exist.

    The longest part that is a project module must define the next name, unless
    it imports everything from somewhere.
    """
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        source = find(".".join(parts[:end]))
        if source is None:
            continue
        if end == len(parts):
            return False
        try:
            statements = names.parse_statements(source)
        except (SyntaxError, ValueError):
            return True
        defined = {name for statement in statements for name in statement.defined}
        stars = any(statement.kind == "star" for statement in statements)
        return parts[end] not in defined and not stars
    return False
