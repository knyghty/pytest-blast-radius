"""Work out which rendered lines of a Django template a change reaches."""

from collections.abc import Iterable
from dataclasses import dataclass

# The name of a template's key in a recording, which no Python function can have.
TEMPLATE = "template:"
# Tags that change how the whole template parses or renders.
WHOLE = {"extends", "load"}
# Tags that continue the block they are in.
INNER = {"elif", "else", "empty", "plural"}
# Tags whose contents render as part of them, not as nodes of their own.
RAW = {"blocktrans", "blocktranslate", "comment", "verbatim"}


def node_line(lineno: int, contents: str, *, text: bool) -> int:
    """Return the line a rendered node is recorded by.

    A text node starts where its first visible character is, so that it differs
    from a tag it follows on the same line.
    """
    stripped = contents.lstrip()
    if not text or not stripped:
        return lineno
    return lineno + contents[: len(contents) - len(stripped)].count("\n")


@dataclass(frozen=True)
class _Token:
    """A template token's lines, the line its node is recorded by, and the lines
    of the blocks it opens or is in."""

    start: int
    end: int
    line: int
    name: str | None
    # The line of the block tag the token is in, or None at the top level.
    outer: int | None
    # Whether the token opens a block, and the line of the block an end closes.
    opens: bool = False
    closes: int | None = None
    # The line of the raw block, such as `{% verbatim %}`, that renders the token.
    raw: int | None = None


def _tokens(source: str) -> list[_Token] | None:
    try:
        from django.template.base import Lexer  # noqa: PLC0415
        from django.template.base import TokenType  # noqa: PLC0415
    except ImportError:
        return None
    tokens = Lexer(source).tokenize()
    names = [
        token.contents.split()[0]
        if token.token_type == TokenType.BLOCK and token.contents.split()
        else None
        for token in tokens
    ]
    result = []
    stack: list[tuple[int, str]] = []
    for index, (token, name) in enumerate(zip(tokens, names, strict=True)):
        start = token.lineno
        end = start + token.contents.count("\n")
        line = node_line(start, token.contents, text=token.token_type == TokenType.TEXT)
        outer = stack[-1][0] if stack else None
        raw = next((line for line, tag in stack if tag in RAW), None)
        if name is not None and name.startswith("end") and stack:
            closes = stack.pop()[0]
            outer = stack[-1][0] if stack else None
            result.append(_Token(start, end, line, name, outer, closes=closes))
            continue
        opens = name is not None and f"end{name}" in names[index + 1 :]
        result.append(_Token(start, end, line, name, outer, opens=opens, raw=raw))
        if opens and name is not None:
            stack.append((start, name))
    return result


def reparses(source: str, spans: Iterable[tuple[int, int]]) -> bool:
    """Return whether changed lines of a template's new source hold a tag that
    changes how the whole template parses, as an added `{% load %}` does."""
    tokens = _tokens(source)
    return tokens is None or any(
        token.name in WHOLE
        for first, count in spans
        for token in tokens
        if token.start < first + count and token.end >= first
    )


def heads(source: str, spans: Iterable[tuple[int, int]]) -> frozenset[int] | None:
    """Return the lines of the rendered nodes that changed lines reach.

    `spans` are the changed lines, as a first line and a count; a count of 0
    inserts after the line. A rendered node is recorded by the line node_line
    gives. A changed text, variable or tag reaches its own node; a changed end or `else`
    of a block, or code inserted into one, reaches the block's tag. None means
    every render of the template, as for a change to `{% load %}` or
    `{% extends %}`, or code added at the top level.
    """
    tokens = _tokens(source)
    if tokens is None:
        return None
    reached: set[int] = set()

    def reach_block(block: int) -> None:
        # A child template's block renders its contents without itself.
        reached.add(block)
        reached.update(token.line for token in tokens if token.outer == block)

    for first, count in spans:
        if not count:
            around = [token for token in tokens if token.start <= first <= token.end]
            block = around[-1].start if around and around[-1].opens else None
            block = block or (around[-1].outer if around else None)
            if block is None:
                return None
            reach_block(block)
            continue
        for token in tokens:
            if token.end < first or token.start >= first + count:
                continue
            if token.name in WHOLE:
                return None
            if token.raw is not None:
                reached.add(token.raw)
                continue
            block = token.closes or (token.outer if token.name in INNER else None)
            if token.closes is None and token.name not in INNER:
                reached.add(token.line)
            elif block is None:
                return None
            else:
                reach_block(block)
    return frozenset(reached)
