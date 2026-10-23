"""Report the changed lines that the tests that ran didn't cover."""

import itertools
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pytest_blast_radius.selection import Hunk


def changed_lines(changes: dict[str, list[Hunk] | None]) -> dict[str, set[int]]:
    """Return the lines each changed Python file has new or changed."""
    return {
        path: {
            line
            for hunk in hunks
            for line in range(hunk.new_start, hunk.new_start + hunk.new_count)
        }
        for path, hunks in changes.items()
        if path.endswith(".py") and hunks
    }


def spans(lines: Iterable[int]) -> str:
    """Return lines as ranges, such as "3-5, 9"."""
    result = []
    for _, group in itertools.groupby(
        enumerate(sorted(lines)), lambda pair: pair[1] - pair[0]
    ):
        numbers = [line for _, line in group]
        first, last = numbers[0], numbers[-1]
        result.append(str(first) if first == last else f"{first}-{last}")
    return ", ".join(result)


def report(
    cov: Any,  # noqa: ANN401
    root: Path,
    changes: dict[str, list[Hunk] | None],
) -> list[str]:
    """Return lines describing how much of the change the tests that ran cover.

    Only the selected tests can run changed lines, so their coverage of them is
    the whole suite's. A file counts if coverage measured it or would report it
    unrun, through its `source` setting. A changed line counts as a statement
    coverage knows, and as partial when it ran without taking every branch, as
    codecov counts them.
    """
    measured = {Path(name) for name in cov.get_data().measured_files()}
    sources = [root / source.replace(".", "/") for source in cov.config.source or []]
    counted = covered = 0
    missed: list[str] = []
    for path, lines in sorted(changed_lines(changes).items()):
        absolute = root / path
        if not absolute.exists() or not (
            absolute in measured
            or any(absolute.is_relative_to(source) for source in sources)
        ):
            continue
        _, statements, _, missing, _ = cov.analysis2(str(absolute))
        changed = lines & set(statements)
        uncovered = changed & set(missing)
        branches = cov._analyze(str(absolute)).missing_branch_arcs()
        partial = {line for line in changed - uncovered if line in branches}
        counted += len(changed)
        covered += len(changed - uncovered - partial)
        if uncovered or partial:
            details = [spans(uncovered)] if uncovered else []
            details += [f"partly {spans(partial)}"] if partial else []
            missed.append(f"- {path}: {', '.join(details)}")
    if not counted:
        return []
    if not missed:
        return [f"blast-radius: tests cover all {counted} changed lines"]
    return [
        f"blast-radius: tests cover {covered} of {counted} changed lines; not:",
        *missed,
    ]
