"""Find the models a Django migration's operations change."""

import ast

# Operations that change only Django's idea of a model, not the database.
STATE_ONLY = {"AlterModelManagers", "AlterModelOptions"}
# Operations that name the model they change `name`, rather than `model_name`.
NAMED = {
    "AlterIndexTogether",
    "AlterModelTable",
    "AlterModelTableComment",
    "AlterOrderWithRespectTo",
    "AlterUniqueTogether",
    "CreateModel",
    "DeleteModel",
}
# Django's operations that take `model_name` first.
FIELDS = {
    "AddConstraint",
    "AddField",
    "AddIndex",
    "AlterConstraint",
    "AlterField",
    "RemoveConstraint",
    "RemoveField",
    "RemoveIndex",
    "RenameField",
    "RenameIndex",
}


def models(source: str | None) -> set[str] | None:
    """Return the lower-case names of the models a migration's operations change.

    None means it may change anything, as code it runs may.
    """
    if source is None:
        return set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: set[str] = set()
    for node in ast.walk(tree):
        # Operations built any other way, as with `operations += [...]`, are
        # unknown.
        if (
            isinstance(node, ast.AnnAssign | ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "operations"
        ) or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "operations"
        ):
            return None
        if not (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "operations"
                for target in node.targets
            )
        ):
            continue
        if not isinstance(node.value, ast.List | ast.Tuple):
            return None
        for operation in node.value.elts:
            names = _operation(operation)
            if names is None:
                return None
            found |= names
    return found


def _operation(node: ast.expr) -> set[str] | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    kind = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else ""
    )
    if kind in STATE_ONLY:
        return set()
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    if kind == "RenameModel":
        values = [
            keywords.get("old_name", node.args[0] if node.args else None),
            keywords.get("new_name", node.args[1] if len(node.args) > 1 else None),
        ]
    else:
        key = "name" if kind in NAMED else "model_name"
        positional = node.args[0] if node.args and kind in NAMED | FIELDS else None
        values = [keywords.get(key, positional)]
    if not all(
        isinstance(value, ast.Constant) and isinstance(value.value, str)
        for value in values
    ):
        return None
    return {value.value.lower() for value in values}  # ty: ignore[unresolved-attribute]
