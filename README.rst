===================
pytest-blast-radius
===================

Run only the tests affected by your changes.

Each run records which lines, functions, classes, templates and data files each
test uses. The next run diffs the working tree against the closest recording and
runs the tests that used anything that changed and any test the recording has
not seen.

Tests that failed last time fail again. A failure may cut a test short or come
from something missing, so they run if anything they use changed, if a module
they ran imports changed code, if a file was added, or if named on the command
line. Otherwise their last failure is reported again, marked as reused. Tests a
skip or an expected failure stopped early run under the same conditions. Errors
in setup or teardown, and failures in runs that left tests out by name, as
``-k``, ``-m``, ``--deselect``, ``--lf`` or ``file.py::test`` do, always run
again.

Why
---

Coding agents run the test suite after every change. Most changes touch a small
part of a project, so most of that time goes on tests that cannot fail.
pytest-blast-radius is for those local runs. CI should still run everything.

It records with ``sys.monitoring``, so it works alongside coverage.py and
pytest-cov, including branch coverage, and with pytest-xdist.

Installation
------------

Python 3.12 or later is required.

.. code-block:: console

    $ pip install pytest-blast-radius

To use it in a project without adding a dependency:

.. code-block:: console

    $ uv run --with pytest-blast-radius pytest --blast-radius

Usage
-----

``--blast-radius``
    Run the tests affected by changes since the closest recording. With no
    recording, or when a change could affect any test, run everything.

``--blast-radius-record``
    Run every test.

``--blast-radius-check``
    Run every test and list the failures ``--blast-radius`` would have skipped.

Test files whose tests are all skipped are not collected, which saves importing
them, unless a file was added, which could add tests to any, or they import a
module whose top level changed, which may no longer import.

With pytest-cov, a run that skips tests reports which changed lines the
tests that ran didn't cover, instead of pytest-cov's report on the whole
project, which would show most code as not run and takes long to write. Only
the tests that ran can run changed lines, so this is what running every test
would report. Runs of every test, as in CI, keep pytest-cov's report.

Every run records the working tree as git sees it, with uncommitted and
untracked files but not ignored ones. Files marked assume-unchanged or
skip-worktree count as they are on disk, clean filters are bypassed, and
submodules and nested repositories count with their uncommitted changes to
tracked files. A changed submodule or symlink to a directory runs the tests
that used files under it. Tests a change does not affect keep their
records. Affected tests that did not run, for example because of ``-k``, run
next time. A run that changes files while it runs, only collects tests or only
sets up fixtures records nothing, nor does ``--lf`` record what files collect.

A recording also holds the Python version and the versions of the packages the
tests imported, and of pytest plugins, which modules the project imports that
weren't installed, and pytest's options that change how tests end, such as
``-W``, ``-o``, ``-p``, ``-c``, ``--runxfail`` and ``--noconftest``. It is only
used while those stay as they were, so changing another package, or a
lockfile, runs no tests.

Recordings
----------

Recordings live in ``.git/blast-radius`` in the main work tree, so worktrees
share them. The closest recording is the one with the fewest changed files,
from any branch. The 100 most recently used are kept. To remove them all:

.. code-block:: console

    $ rm -r "$(git rev-parse --git-common-dir)/blast-radius"

To start from CI's recordings, record on the main branch in CI and copy the
directory into place locally, for example from a cached artifact:

.. code-block:: console

    $ pytest --blast-radius-record
    $ cp -r "$(git rev-parse --git-common-dir)/blast-radius" recordings

Recordings refer to git trees and files by path, so they work in any clone of
the repository. CI must install the same Python and package versions for them to
be used.

How it selects
--------------

A change runs:

* every test, when it changes pytest's settings, adds or removes a
  ``conftest.py``, changes a pytest hook, ``pytestmark`` or an autouse fixture,
  touches a file matching ``blast_radius_full_run``, changes code that runs
  outside tests such as ``AppConfig.ready()`` (code that runs while pytest
  collects a test file, such as a ``pytest_generate_tests`` helper, counts as
  that file's), removes a name another module
  needs to load, or changes the top level of a module that only a framework
  loads and the recording can't tie to tests (a module project code loads by
  name, as with ``importlib.import_module()``, counts as imported by that
  code, or by the module whose top level called it);
* for a change inside a function, the tests that ran the changed statement,
  including through a wider-scoped fixture or a hit in a ``functools`` cache or
  Django's local memory cache, which credits what computing the cached value
  ran, even in another test; a change to a ``def``
  line, a decorator, a docstring or a ``global``
  statement runs every test that called the function, and, unless only its
  annotations changed, everything built by calling it at import time;
* for a changed top-level name, such as a constant, a class body, an import or
  a decorator's arguments, the tests that ran code mentioning it, ran the
  class's methods or ran a method for one of its instances;
* the same for what is built from the name at import time: values computed
  from a changed value, subclasses, classes that assign a changed class, as in
  ``Meta.model``, and values read from its attributes, but not what merely
  holds or calls it, such as a registry or ``View.as_view()``; a changed class
  that gains or loses a special method, or inherits from a library class that
  no Django hook credits, such as a form field or widget, also runs the tests
  that used it or an instance made at import time, and
  everything built at import time by its, its ancestors' or its subclasses'
  methods, as ``Child.build()`` or ``__init_subclass__``;
* for a method added to a class, the tests that used the class or a subclass
  and ran code mentioning the name as an attribute or a string, ran a project
  ancestor's method of that name, or read a template mentioning it, unless it
  overrides something inherited from outside the project, is abstract, belongs
  to a protocol or is a name frameworks look for, such as a view's ``post``, a
  form's ``clean_<field>``, a middleware's ``process_view`` or a model field's
  ``from_db_value``; an
  override of one of Django's model, form, view or admin methods runs the tests
  in which Django called it on the class or a subclass;
* for a changed class attribute, the tests that used the class, a subclass or an
  instance made at import time, if the class's ancestors are all project or
  builtin classes, without a metaclass, ``__init_subclass__``, decorator or
  tuple base, and nothing reads it at import time, and everything built at
  import time by code mentioning it;
* for other top-level changes, such as a call that registers something, a
  newly registering decorator, new arguments to a decorator that may register,
  or a changed value they read, as in ``@receiver(pre_save, sender=Order)`` or
  ``@pytest.fixture(autouse=ENABLED)``, outside test files, or a name
  that shadows a builtin or adds a
  module ``__getattr__``, the tests that used that module or any module that
  imports it;
* for an enum's members, as in a ``TextChoices``, what a changed value reaches,
  as what holds the enum may copy them;
* the tests that read a changed data file, or a Python file as text; for a
  changed Django template, the tests that rendered a changed node, or the block
  holding a change, and every test that rendered it when ``{% load %}`` or
  ``{% extends %}`` changes;
* for an added or removed file, the tests that listed its directory or checked
  for it, as a glob, ``os.path.exists()`` or ``os.access()`` does, and the same
  for each directory it adds or removes with it, and for an
  added module, the tests that used a module it may replace, as a package
  replaces a module of the same name;
* every test the recording has not seen.

Test files and ``conftest.py`` follow the same rules, so changing one test runs
that test, and changing a fixture runs the tests that used it. Adding,
removing or renaming a fixture, or changing its decorator or parameters, runs
the tests that request one by that name where the fixture's file reaches.

With Django, recording also credits each test with the models, forms,
class-based views and admin classes it uses through Django, and the
factory_boy factories it builds, including through ``SubFactory``, so a change
to a model field, a view attribute or a factory declaration runs the tests that
used that class. It credits
each test with the paths it requests and the URL names it reverses, in each
URLconf they go through, so a change to ``path()`` entries runs the tests that
requested a path the old or new route matches or reversed the old or new name.
Any other change to a ``urls.py``, or one that may stop it loading, runs every
test that used URLs. A change to a template tag library runs the tests that
rendered templates loading it, or every template for a builtin one; a change
to a management command, the tests that called it; a changed setting in the
settings module, or a module it imports from, the tests that read it or a
setting computed from it, unless it is read outside tests or is one Django
builds something from once and keeps, such as ``TEMPLATES`` or ``CACHES``; and a
changed migration, the tests that set up the test database with migrations and
the tests that used the models its operations change, or every test if it runs
code such as ``RunPython``.

Changes to comments, formatting, ``if TYPE_CHECKING:`` blocks and local
variable annotations change nothing.

Configuration
-------------

Settings go in ``[tool.pytest]`` or ``[tool.pytest.ini_options]`` in
``pyproject.toml``.

``blast_radius_full_run``
~~~~~~~~~~~~~~~~~~~~~~~~~

Default: ``[]``

Globs of files whose change runs every test, matched against the path or the
file name. With Django installed, these add to ``*/migrations/*``,
``settings.py`` and ``*/settings/*``, unless the recording saw which tests set
up the database or read each setting, and to ``*.mo`` files added or removed,
as Django keeps which languages have one; a changed one runs the tests that
translated with it. Changes to ``pyproject.toml``,
``setup.cfg``, ``tox.ini`` and ``pytest.ini`` run every test when they change
pytest's settings.

``blast_radius_also_run``
~~~~~~~~~~~~~~~~~~~~~~~~~

Default: ``[]``

``"changed-glob: test-glob"`` entries. A change to a file matching the first
glob runs the tests in files matching the second. Use it for tests that inspect
code without calling it, such as a missing migrations check:

.. code-block:: toml

    [tool.pytest]
    blast_radius_also_run = ["models.py: */test_migrations.py"]

``blast_radius_serial_below``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Default: ``100``

With pytest-xdist, run in one process when fewer tests than this are selected.
Starting workers takes longer than running a few tests.

``blast_radius_prioritise``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Default: ``false``

Run last run's failures first, then new tests, then the rest, fastest first,
and reused failures last, so ``-x`` stops as early as it can. Tests in one
module may then set up module and class fixtures more than once.

``blast_radius_reuse_failures``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Default: ``true``

Report a failed test's last failure instead of running it again while nothing
it uses changes. Turn it off for tests that fail intermittently.

``blast_radius_keep_coverage``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Default: ``false``

Keep pytest-cov's own report, and its ``fail_under`` check, in runs that skip
tests.

Limitations
-----------

* Changing the Python version, a package the tests import or a pytest plugin
  runs every test.
* Code that runs in subprocesses is not recorded, nor are files that C
  extensions read without an audit event, apart from SQLite databases.
* A test whose outcome depends on the date or time, the network or anything
  else outside the repository runs only when something it uses changes.
* State one test leaves for another, such as a value a module global, a cache
  backend other than local memory, or a ``cached_property`` on a module-level
  object keeps, a registry it
  fills or a generator it advances, is credited only to the test that ran the
  code producing it. A failure such state caused is reused until something the
  failing test uses changes, and a test can lose state that an unaffected test
  file sets up as it is imported, since unaffected files are not collected.
* Recording replaces ``os.stat``, ``os.lstat``, ``os.access`` and
  ``importlib.util.find_spec`` with wrappers, runs code of its own as methods
  are called, which lowers how deep they can recurse, and connects to Django's
  ``pre_init`` signal. Tests that check those functions' identity, recurse to
  the limit or count the signal's receivers may behave differently.
* Other plugins' options, such as pytest-django's ``--nomigrations``, are not
  compared, so record and select with the same ones.
* Settings and data files that third-party code reads outside tests, such as
  coverage settings, are not compared.
* Files ignored by git are not compared, so a changed ``.env`` goes unnoticed.
* Selecting by name assumes that what holds or calls a class at import time
  reads its attributes later, through code the recording credits. A framework
  that copies a class's attributes when it registers the class, without
  calling project code or one of the Django hooks, can cause misses.
* A template change that stops the template compiling, such as an unknown
  filter, runs only the tests that rendered the changed part.
* A call in a top-level assignment that changes state elsewhere, as in
  ``_ = handlers.add("key", handler)``, is followed only through the names the
  assignment binds.
* A class that registers itself through ``__init_subclass__`` or a metaclass
  other than Django's is not noticed when it is added.
* A method added to a class is not noticed by code that finds it through a name
  it builds, such as ``getattr(obj, f"render_{kind}")``, apart from the Django
  and pytest names listed in ``selection.DISPATCHED``.

Run ``--blast-radius-check`` now and then to measure misses on your project.
