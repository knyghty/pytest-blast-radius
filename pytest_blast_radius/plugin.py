"""Choose the tests to run and record what each test uses."""

import argparse
import importlib.util
import inspect
import json
import os
import types
from collections.abc import Generator
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Protocol

import pytest

from pytest_blast_radius import patch
from pytest_blast_radius import recording
from pytest_blast_radius import selection
from pytest_blast_radius import storage

KEY = "blast_radius"
# How much of a failure's report to keep, to show when reusing it.
FAILURE_LINES = 60
# Only their pytest settings count. Lockfiles change nothing until packages change,
# which the environment check catches.
FULL_RUN = ["pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini"]
# Django keeps translation catalogs for the whole process, once a test loads them.
DJANGO_FULL_RUN = ["*/migrations/*", "settings.py", "*/settings/*", "*.mo"]
# Plugins that change how tests run or report, not how they end.
REPORTING = {"sugar", "xdist"}
# Where prioritising puts tests: new tests, between failures and the rest, and
# reused failures.
NEW = [1, 0.0]
RUN_LAST = 3


def full_run_globs() -> list[str]:
    """Return the built-in globs of files whose change runs every test."""
    django = importlib.util.find_spec("django") is not None
    return FULL_RUN + (DJANGO_FULL_RUN if django else [])


@dataclass
class State:
    """What this run selects and records."""

    mode: str
    repository: storage.Repository | None = None
    recorder: recording.Recorder | None = None
    record: bool = False
    tree: str = ""
    packages: dict[str, str] = field(default_factory=dict)
    message: str = ""
    base: dict[str, Any] | None = None
    changes: dict[str, list[selection.Hunk] | None] = field(default_factory=dict)
    affected: set[str] = field(default_factory=set)
    deselect: set[str] = field(default_factory=set)
    # Test files, relative to the repository, with nothing to collect.
    skip: set[str] = field(default_factory=set)
    # The tests each test file collected, relative to the repository.
    collected: dict[str, list[str]] = field(default_factory=dict)
    # Whether to report coverage of the changed lines instead of pytest-cov's.
    patch: bool = False
    # How long each test took, in seconds.
    durations: dict[str, float] = field(default_factory=dict)
    # Each selected test's place in the run, as [group, duration], when prioritising.
    order: dict[str, list[float]] = field(default_factory=dict)
    would_deselect: set[str] | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    # Each failed test's report, and the failures reused instead of running.
    failed: dict[str, str] = field(default_factory=dict)
    reused: dict[str, str] = field(default_factory=dict)
    # Tests a skip or an expected failure stopped before their end.
    cut: set[str] = field(default_factory=set)
    # Failures to run again rather than reuse: errors outside a test's call, which
    # a reused failure can't report as such, and failures in runs that left out
    # tests by name, which may have set up what the test needs.
    rerun: set[str] = field(default_factory=set)
    # The project files each failure's traceback goes through.
    failed_in: dict[str, set[str]] = field(default_factory=dict)


STATE = pytest.StashKey[State]()
FIXTURE_NAMES = pytest.StashKey[list[str]]()


class Node(Protocol):
    """The parts of an xdist WorkerController this plugin uses."""

    config: pytest.Config
    workerinput: dict[str, Any]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the command line flags and configuration settings."""
    group = parser.getgroup("blast-radius")
    group.addoption(
        "--blast-radius",
        action="store_true",
        help="run only the tests affected by changes since the last recorded run",
    )
    group.addoption(
        "--blast-radius-record",
        action="store_true",
        help="run every test and record what each one uses",
    )
    group.addoption(
        "--blast-radius-check",
        action="store_true",
        help="run every test and report failures --blast-radius would have skipped",
    )
    parser.addini(
        "blast_radius_full_run",
        "extra globs of files whose change runs every test",
        type="linelist",
        default=[],
    )
    parser.addini(
        "blast_radius_also_run",
        "'changed-glob: test-glob' lines: a change matching the first runs tests in "
        "files matching the second",
        type="linelist",
        default=[],
    )
    parser.addini(
        "blast_radius_serial_below",
        "run without pytest-xdist workers when fewer tests than this are selected",
        type="int",
        default=100,
    )
    parser.addini(
        "blast_radius_prioritise",
        "run the tests most likely to fail first: last run's failures, then new "
        "tests, then the rest, fastest first, and reused failures last",
        type="bool",
        default=False,
    )
    parser.addini(
        "blast_radius_reuse_failures",
        "report a failed test's last failure instead of running it again, while "
        "nothing it uses changes",
        type="bool",
        default=True,
    )
    parser.addini(
        "blast_radius_keep_coverage",
        "keep pytest-cov on when tests are skipped, though its report only covers "
        "the tests that ran",
        type="bool",
        default=False,
    )


def _mode(namespace: argparse.Namespace) -> str | None:
    if namespace.blast_radius_check:
        return "check"
    if namespace.blast_radius_record:
        return "record"
    if namespace.blast_radius:
        return "select"
    return None


def _state(config: pytest.Config, namespace: argparse.Namespace) -> State | None:
    if STATE in config.stash:
        return config.stash[STATE]
    mode = _mode(namespace)
    if mode is None:
        return None
    state = config.stash[STATE] = State(mode)
    try:
        state.repository = storage.find_repository(config.rootpath)
    except storage.GitError:
        return state
    state.recorder = recording.Recorder(state.repository.root)
    state.recorder.start()
    return state


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Start recording before conftest files and Django settings load."""
    _state(early_config, early_config.known_args_namespace)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Decide what to select and whether to keep recording."""
    state = _state(config, config.option)
    if state is None:
        return
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        info = workerinput.get(KEY, {})
        state.record = info.get("record", False)
        state.deselect = set(info.get("deselect", ()))
        state.skip = set(info.get("skip", ()))
        state.reused = info.get("reused", {})
        state.order = info.get("order", {})
    else:
        _decide(config, state)
        config.pluginmanager.register(
            Outcomes(state, _alone(config), config.invocation_params.dir)
        )
    if state.reused:
        config.pluginmanager.register(ReusedFailures(state.reused))
    if state.recorder is None:
        return
    if state.record:
        state.recorder.connect_django()
    else:
        state.recorder.stop()
        state.recorder = None


def _decide(config: pytest.Config, state: State) -> None:
    repository = state.repository
    if repository is None:
        state.message = "running all tests: not in a git repository"
        return
    state.packages = {**storage.installed(), "options": _options(config)}
    try:
        state.tree = repository.snapshot()
        base = repository.load(state.tree, state.packages)
    except storage.GitError as error:
        state.message = f"running all tests: git failed: {error}"
        return
    # Collecting runs no test, so it learns nothing and would only forget tests.
    # Nor does setting up fixtures alone run the tests.
    state.record = not (
        config.option.collectonly
        or getattr(config.option, "setuponly", False)
        or getattr(config.option, "setupplan", False)
    )
    if state.mode == "record":
        # With no changes, tests left out of a narrowed run keep their records.
        if base is not None and base["tree"] == state.tree:
            state.base = base
        state.message = "running all tests, recording"
        return
    if base is None:
        state.message = (
            "running all tests (no recorded run with this Python and these packages)"
        )
        return
    try:
        changes = _changes(repository, base["tree"], state.tree)
        affected, selector = _select(config, repository, base, changes)
    except pytest.UsageError:
        raise
    # A bug in selection runs every test rather than stopping the run.
    except Exception as error:  # noqa: BLE001
        reason = (
            str(error)
            if isinstance(error, selection.RunAllError)
            else f"selection failed: {error!r}"
        )
        state.message = f"running all tests ({reason})"
        return
    # Tests that failed last time fail again, so a failure never goes unseen. They
    # run again when something they use changed, or they are asked for by name;
    # otherwise they would fail the same way, so their failure is reused. A failure
    # cuts a test's recording short, though, so a module it ran may now load
    # differently, and it can come from something missing, such as a module or
    # file, which leaves nothing in the recording to compare.
    failures: dict[str, str] = base["failed"]
    # Tests a skip or an expected failure cut short stop early too.
    stopped = failures.keys() | set(base["cut"])
    added = any(repository.source(base["tree"], path) is None for path in changes)
    rerun = affected | (
        stopped
        if added
        else selector.reaching(
            stopped,
            changes,
            base["failed_in"],
            lambda path: _read(repository.root / path),
        )
    )
    reused = (
        {
            nodeid: text
            for nodeid, text in failures.items()
            if nodeid not in rerun
            and nodeid not in base["rerun"]
            and not _requested(config, nodeid)
        }
        if state.mode == "select" and config.getini("blast_radius_reuse_failures")
        else {}
    )
    selected = rerun | failures.keys()
    deselect = set(base["tests"]) - selected
    changed = f"{len(changes)} changed files"
    if state.mode == "check":
        state.would_deselect = deselect
        state.message = f"running all tests to check selection for {changed}"
        return
    state.base = base
    state.changes = changes
    # Failures that rerun count as affected, so a run that leaves them out drops
    # their records and they run next time.
    state.affected = rerun
    state.deselect = deselect
    # A file needs collecting unless every test it collected last time is known and
    # deselected. A changed file may hold new tests, and a change that could change
    # what an unchanged one collects, such as parametrize arguments, selects its
    # tests. An added file may add tests to any, as through a glob, and one that
    # imports a changed module shows whether that module still imports.
    state.skip = {
        path
        for path, nodeids in base["collected"].items()
        if not added
        and path not in changes
        and path not in selector.collect
        and deselect.issuperset(nodeids)
    }
    state.message = f"skipping {len(deselect)} tests unaffected by {changed}"
    state.reused = reused
    if reused:
        state.message += f", reusing {len(reused)} failures"
    if config.getini("blast_radius_prioritise"):
        state.order = _order(selected, failures, reused, base["durations"])
        state.message += ", likeliest failures first"
    _prepare(config, state, len(base["tests"]) - len(deselect))


def _prepare(config: pytest.Config, state: State, selected: int) -> None:
    """Set up coverage and workers for a run of only the selected tests."""
    if config.pluginmanager.has_plugin("_cov") and not config.getini(
        "blast_radius_keep_coverage"
    ):
        _report_changed_lines_only(config)
        state.patch = True
        state.message += ", reporting coverage of changed lines"
    if selected < int(config.getini("blast_radius_serial_below")) and getattr(
        config.option, "numprocesses", None
    ):
        _disable_xdist(config)
        state.message += ", in one process"


def _options(config: pytest.Config) -> str:
    """Return the command line options that can change how tests end, which a
    recording must have been made with to be used."""
    option = config.option
    return json.dumps(
        {
            "confcutdir": str(getattr(option, "confcutdir", None)),
            "inifile": str(getattr(option, "inifilename", None)),
            "noconftest": getattr(option, "noconftest", False),
            "plugins": sorted(
                name
                for name in option.plugins or []
                if name.removeprefix("no:") not in REPORTING
            ),
            "import_mode": getattr(option, "importmode", None),
            # Blast-radius's own settings choose tests, not how they end.
            "override_ini": sorted(
                line
                for line in option.override_ini or []
                if not line.startswith("blast_radius_")
            ),
            "runxfail": getattr(option, "runxfail", False),
            "strict_markers": getattr(option, "strict_markers", False),
            "warnings": option.pythonwarnings or [],
        },
        sort_keys=True,
    )


def _order(
    selected: set[str],
    failures: dict[str, str],
    reused: dict[str, str],
    durations: dict[str, float],
) -> dict[str, list[float]]:
    # Failures that run again come first, then new tests, then the rest, so `-x`
    # stops as early as it can. Reused failures are already known.
    return {
        nodeid: [
            RUN_LAST if nodeid in reused else 0 if nodeid in failures else 2,
            durations.get(nodeid, 0.0),
        ]
        for nodeid in selected
    }


def _report_changed_lines_only(config: pytest.Config) -> None:
    # A report on the few tests that ran says most code is uncovered, and takes
    # longer than they do, so pytest-cov keeps measuring without reporting or
    # failing under a total. It reads these from the options it parsed before
    # conftest files loaded.
    for options in (config.option, config.known_args_namespace):
        options.cov_report = {}
        options.cov_fail_under = None


def _requested(config: pytest.Config, nodeid: str) -> bool:
    """Return whether the command line names a test or its file."""
    for argument in config.args:
        path, separator, rest = argument.partition("::")
        # Node ids are relative to the rootdir, and arguments to where pytest ran.
        absolute = Path(os.path.normpath(config.invocation_params.dir / path))
        if not absolute.is_relative_to(config.rootpath):
            continue
        named = absolute.relative_to(config.rootpath).as_posix() + separator + rest
        if nodeid == named or nodeid.startswith((f"{named}::", f"{named}[")):
            return True
    return False


def _alone(config: pytest.Config) -> bool:
    """Return whether a run leaves out tests by name or outcome, as `-k`, `-m`,
    `--deselect`, `--lf` or `file.py::test` do."""
    option = config.option
    return bool(
        option.keyword
        or option.markexpr
        or option.deselect
        or getattr(option, "lf", False)
        or any("::" in argument for argument in config.args)
    )


def _disable_xdist(config: pytest.Config) -> None:
    # Starting workers costs more than it saves for a few tests. This runs before
    # xdist's pytest_configure, which starts them.
    config.option.numprocesses = 0
    config.option.dist = "no"
    config.option.tx = []


def _changes(
    repository: storage.Repository, base: str, tree: str
) -> dict[str, list[selection.Hunk] | None]:
    try:
        return selection.find_changes(repository, base, tree)
    except storage.GitError as error:
        msg = f"git failed: {error}"
        raise selection.RunAllError(msg) from error


def _select(
    config: pytest.Config,
    repository: storage.Repository,
    data: dict[str, Any],
    changes: dict[str, list[selection.Hunk] | None],
) -> tuple[set[str], selection.Selector]:
    also_run = []
    for line in config.getini("blast_radius_also_run"):
        changed, separator, tests = line.partition(":")
        if not separator:
            msg = (
                "blast_radius_also_run entries look like 'changed-glob: test-glob', "
                f"not {line!r}"
            )
            raise pytest.UsageError(msg)
        also_run.append((changed.strip(), tests.strip()))
    selector = selection.Selector(
        data,
        full_run_globs() + config.getini("blast_radius_full_run"),
        also_run,
        config.getini("python_files"),
    )
    affected = selector.select(
        changes,
        lambda path: repository.source(data["tree"], path),
        lambda path: _read(repository.root / path),
    )
    return affected, selector


def _read(path: Path) -> str | None:
    """Return a file's text, nothing for a directory, as git shows a tree, or None
    if the path doesn't exist."""
    if path.is_dir():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


def pytest_report_header(config: pytest.Config) -> str | None:
    """Say what blast-radius selects and records."""
    state = config.stash.get(STATE, None)
    if state is None or not state.message:
        return None
    return f"blast-radius: {state.message}"


def _relative(repository: storage.Repository, path: Path) -> str:
    return Path(os.path.relpath(path.resolve(), repository.root)).as_posix()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """Credit what collecting a test module runs to it, and stop crediting one whose
    import a skip or error ended."""
    recorder = _recorder(collector.config)
    module = collector.getparent(pytest.Module)
    path = recorder.relative(str(module.path)) if recorder and module else None
    if recorder is not None and path is not None:
        recorder.start_collecting(path)
    try:
        return (yield)
    finally:
        if recorder is not None:
            recorder.prune_modules()
            if path is not None:
                recorder.stop_collecting()


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Skip test files whose tests are all deselected, which saves importing them."""
    state = config.stash.get(STATE, None)
    if state is None or state.repository is None or not state.skip:
        return None
    return _relative(state.repository, collection_path) in state.skip or None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Note what each file collected, then deselect the tests no change affects.

    This runs before options such as `-k` deselect anything.
    """
    state = config.stash.get(STATE, None)
    if state is None or state.repository is None:
        return
    # Files named with a test, as in `test_a.py::test_b`, collect only that.
    narrowed = {
        _relative(
            state.repository,
            config.invocation_params.dir / argument.partition("::")[0],
        )
        for argument in config.args
        if "::" in argument
    }
    # `--lf` leaves out of each file the tests that passed last time.
    last_failed = bool(getattr(config.option, "lf", False))
    for item in items:
        path = _relative(state.repository, item.path)
        if path not in narrowed and not last_failed:
            state.collected.setdefault(path, []).append(item.nodeid)
    if state.order:
        items.sort(key=lambda item: state.order.get(item.nodeid, NEW))
    if not state.deselect:
        return
    deselected = [item for item in items if item.nodeid in state.deselect]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.nodeid not in state.deselect]


def _recorder(config: pytest.Config) -> recording.Recorder | None:
    state = config.stash.get(STATE, None)
    return None if state is None else state.recorder


# Plugins must hash, as pluggy keeps them in sets.
@dataclass(eq=False)
class ReusedFailures:
    """Report reused failures instead of running their tests."""

    reused: dict[str, str]

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> bool | None:
        """Report a reused failure instead of running the test."""
        text = self.reused.get(item.nodeid)
        if text is None:
            return None
        ihook = item.ihook
        ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        report = pytest.TestReport(
            item.nodeid,
            item.location,
            dict.fromkeys(item.keywords, 1),
            "failed",
            f"{text}\n\nblast-radius: reused this failure from an earlier run, as "
            "nothing the test uses has changed since",
            "call",
            [],
        )
        ihook.pytest_runtest_logreport(report=report)
        # The test before this one left set up what this one shares with it, such
        # as its module, so tear down what the next test doesn't, as running it
        # would have.
        item.session._setupstate.teardown_exact(nextitem)
        ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """Credit each test with what its setup, call and teardown use."""
    recorder = _recorder(item.config)
    state = item.config.stash.get(STATE, None)
    if recorder is None or (state is not None and item.nodeid in state.reused):
        return (yield)
    path = recorder.relative(str(item.path)) or item.nodeid.partition("::")[0]
    recorder.start_test(item.nodeid, path)
    function = getattr(item, "obj", None)
    code = getattr(inspect.unwrap(function), "__code__", None) if function else None
    if isinstance(code, types.CodeType):
        recorder.credit_definition(code)
    try:
        return (yield)
    finally:
        recorder.finish_test(
            item.stash.get(FIXTURE_NAMES, None) or getattr(item, "fixturenames", ())
        )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Note the fixtures a test used, including those it asked for by
    `request.getfixturevalue()`, before pytest forgets them."""
    names = getattr(getattr(item, "_request", None), "fixturenames", None)
    if names is not None and _recorder(item.config) is not None:
        item.stash[FIXTURE_NAMES] = names


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
) -> Generator[None, object, object]:
    """Keep what wider-scoped fixtures use, to credit every test that uses them."""
    recorder = _recorder(request.config)
    if recorder is None or fixturedef.scope == "function":
        return (yield)
    keys = recorder.start_fixture()
    try:
        return (yield)
    finally:
        recorder.finish_fixture(fixturedef, keys)


def pytest_fixture_post_finalizer(
    fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
) -> None:
    """Forget torn-down fixtures."""
    recorder = _recorder(request.config)
    if recorder is not None:
        recorder.drop_fixture(fixturedef)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Node) -> None:
    """Tell xdist workers what to deselect and whether to record."""
    state = node.config.stash.get(STATE, None)
    if state is not None:
        node.workerinput[KEY] = {
            "record": state.record,
            "deselect": sorted(state.deselect),
            "skip": sorted(state.skip),
            "reused": state.reused,
            "order": state.order,
        }


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Node, error: object) -> None:
    """Collect what an xdist worker recorded."""
    state = node.config.stash.get(STATE, None)
    part = getattr(node, "workeroutput", {}).get(KEY)
    if state is not None and part is not None:
        state.parts.append(part)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Save what was recorded, and pass when every test was deselected."""
    config = session.config
    state = config.stash.get(STATE, None)
    if state is None:
        return
    if state.deselect and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
    workeroutput = getattr(config, "workeroutput", None)
    if workeroutput is not None:
        if state.recorder is not None:
            workeroutput[KEY] = {
                **state.recorder.export(),
                "collected": state.collected,
            }
        return
    if (
        state.record
        and state.recorder is not None
        and state.repository is not None
        and session.exitstatus in {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED}
    ):
        _save(state, state.repository, state.recorder)


@dataclass(eq=False)
class Outcomes:
    """Note how each test ended, whichever plugins report it."""

    state: State
    # Whether the run leaves tests out by name, as `-k` does.
    alone: bool
    # Where pytest ran, which traceback paths are relative to.
    directory: Path

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Note a failure's report and the files its traceback goes through, a skip
        or expected failure that cut a test short, and how long each phase took."""
        state = self.state
        nodeid = report.nodeid
        state.durations[nodeid] = state.durations.get(nodeid, 0.0) + report.duration
        if report.failed:
            lines = report.longreprtext.splitlines()[-FAILURE_LINES:]
            state.failed.setdefault(nodeid, "\n".join(lines))
            state.failed_in.setdefault(nodeid, set()).update(
                self.traceback_files(report)
            )
            if self.alone or report.when != "call":
                state.rerun.add(nodeid)
        elif report.skipped:
            state.cut.add(nodeid)

    def traceback_files(self, report: pytest.TestReport) -> set[str]:
        """Return the project files a failure's traceback, including the
        exceptions it was raised from, goes through, relative to the repository."""
        repository = self.state.repository
        chain = getattr(report.longrepr, "chain", None) or []
        paths = {
            entry.reprfileloc.path
            for traceback, _, _ in chain
            for entry in traceback.reprentries
            if getattr(entry, "reprfileloc", None) is not None
        }
        found = set()
        for path in paths:
            absolute = Path(os.path.normpath(self.directory / path))
            if repository is not None and absolute.is_relative_to(repository.root):
                found.add(absolute.relative_to(repository.root).as_posix())
        return found


def _save(
    state: State, repository: storage.Repository, recorder: recording.Recorder
) -> None:
    # A run of no tests on the recorded tree would save the same recording.
    if (
        state.base is not None
        and state.base["tree"] == state.tree
        and not recorder.tests
        and not any(part["tests"] for part in state.parts)
    ):
        return
    # Files may have changed during the run, and then the recording matches no tree.
    if repository.snapshot() != state.tree:
        return
    parts = [*state.parts, {**recorder.export(), "collected": state.collected}]
    ran = {nodeid for part in parts for nodeid in part["tests"]}
    # Reused failures keep their original reports and durations.
    failed = {
        nodeid: text
        for nodeid, text in state.failed.items()
        if nodeid not in state.reused
    }
    durations = {
        nodeid: duration
        for nodeid, duration in state.durations.items()
        if nodeid not in state.reused
    }
    cut = set(state.cut)
    rerun = set(state.rerun)
    failed_in = {
        nodeid: sorted(paths)
        for nodeid, paths in state.failed_in.items()
        if nodeid not in state.reused
    }
    if state.base is not None:
        durations = {**state.base["durations"], **durations}
        cut |= set(state.base["cut"]) - ran
        rerun |= set(state.base["rerun"]) - ran
        failed_in = {
            **{
                nodeid: paths
                for nodeid, paths in state.base["failed_in"].items()
                if nodeid not in ran
            },
            **failed_in,
        }
        moves = {
            path: selection.line_moves(hunks) for path, hunks in state.changes.items()
        }
        parts.insert(0, recording.carry(state.base, state.affected, moves))
        failed = {
            **{
                nodeid: text
                for nodeid, text in state.base["failed"].items()
                if nodeid not in ran
            },
            **failed,
        }
    data = recording.merge(parts)
    files = data["files"]
    data["tests"] = {
        nodeid: test
        for nodeid, test in data["tests"].items()
        if (repository.root / files[test[0]]).exists()
    }
    data["collected"] = {
        path: nodeids
        for path, nodeids in data["collected"].items()
        if (repository.root / path).exists()
    }
    modules = {name: files[index] for name, index in data["modules"].items()}
    analysis = recording.analyse_modules(
        repository.root, modules, repository.load_parsed()
    )
    data.update(
        tree=state.tree,
        environment=storage.environment(
            data["loaded"],
            state.packages,
            {
                module.partition(".")[0]
                for _, imported in analysis.parsed.values()
                for module in imported
                if module
            },
        ),
        edges=analysis.edges,
        references=analysis.references,
        stars=analysis.stars,
        missing=analysis.missing,
        holds=analysis.holds,
        # Only global names the project defines can matter. Attributes and strings
        # may name methods a class gains later.
        names=[
            [index, kept, attributes, strings]
            for index, global_names, attributes, strings in data["names"]
            if (kept := sorted(analysis.defined.intersection(global_names)))
            or attributes
            or strings
        ],
        failed={
            nodeid: text for nodeid, text in failed.items() if nodeid in data["tests"]
        },
        cut=sorted(cut & data["tests"].keys()),
        rerun=sorted(rerun & failed.keys() & data["tests"].keys()),
        failed_in={
            nodeid: paths
            for nodeid, paths in failed_in.items()
            if nodeid in failed and nodeid in data["tests"]
        },
        durations={
            nodeid: round(duration, 4)
            for nodeid, duration in durations.items()
            if nodeid in data["tests"]
        },
    )
    repository.save(data)
    repository.save_parsed(analysis.parsed)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Report failures selection would have skipped, and write a GitHub step summary."""
    state = config.stash.get(STATE, None)
    if state is None or hasattr(config, "workerinput"):
        return
    lines = [f"blast-radius: {state.message}"]
    if state.would_deselect is not None:
        missed = sorted(state.failed.keys() & state.would_deselect)
        skipped = len(state.would_deselect)
        if missed:
            lines.append(
                f"selection would have skipped {skipped} tests, "
                f"including {len(missed)} failing tests:"
            )
            lines.extend(f"- {nodeid}" for nodeid in missed)
        else:
            lines.append(
                "selection would have run every failing test "
                f"and skipped {skipped} tests"
            )
    cov = getattr(
        getattr(config.pluginmanager.get_plugin("_cov"), "cov_controller", None),
        "cov",
        None,
    )
    if state.patch and cov is not None and state.repository is not None:
        lines += patch.report(cov, state.repository.root, state.changes)
    # The header already says what blast-radius selects.
    shown = lines if state.mode == "check" else lines[1:]
    if shown:
        terminalreporter.section("blast-radius")
        for line in shown:
            terminalreporter.write_line(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")


def pytest_unconfigure(config: pytest.Config) -> None:
    """Stop recording."""
    recorder = _recorder(config)
    if recorder is not None:
        recorder.stop()
