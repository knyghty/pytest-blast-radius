"""More changes that once made selection skip a test whose outcome they change."""

from tests.conftest import Project

OTHER = "\n\ndef test_other():\n    pass\n"


def check(
    project: Project, files: dict[str, str], path: str, new: str, expected: set[str]
) -> None:
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=len(expected) + 1)
    project.write({path: new})
    # Without selection, the change fails the expected tests.
    assert project.run().ret != 0
    assert expected <= project.ran("--blast-radius")


def check_fails(project: Project, files: dict[str, str], path: str, new: str) -> None:
    """Check that a run with selection fails after a change that breaks imports."""
    project.commit(files)
    project.run("--blast-radius").assert_outcomes(passed=2)
    project.write({path: new})
    assert project.run("--blast-radius").ret != 0


def test_insert_unindented_line_in_string(project: Project) -> None:
    source = "def text():\n    return '''\\\nhello\n'''\n"
    files = {
        "pkg/__init__.py": "",
        "pkg/t.py": source,
        "tests/test_t.py": (
            "from pkg.t import text\n\n\n"
            "def test_text():\n    assert text() == 'hello\\n'\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/t.py",
        source.replace("hello\n", "hello\nworld\n"),
        {"tests/test_t.py::test_text"},
    )


def test_insert_unindented_line_in_test_string(project: Project) -> None:
    test = "def test_text():\n    assert 'hello\\n' == '''\\\nhello\n'''\n" + OTHER
    check(
        project,
        {"tests/test_t.py": test},
        "tests/test_t.py",
        test.replace("hello\n'''", "hello\nworld\n'''"),
        {"tests/test_t.py::test_text"},
    )


def test_instance_in_default_argument(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": "class Conf:\n    timeout = 1\n",
        "pkg/use.py": (
            "from pkg.conf import Conf\n\n\n"
            "def timeout(conf=Conf()):\n    return conf.timeout\n"
        ),
        "tests/test_u.py": (
            "from pkg.use import timeout\n\n\n"
            "def test_timeout():\n    assert timeout() == 1\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/conf.py",
        "class Conf:\n    timeout = 2\n",
        {"tests/test_u.py::test_timeout"},
    )


def test_instance_in_class_attribute(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": "class Conf:\n    timeout = 1\n",
        "pkg/use.py": (
            "from pkg.conf import Conf\n\n\n"
            "class Service:\n    conf = Conf()\n\n"
            "    def timeout(self):\n        return self.conf.timeout\n"
        ),
        "tests/test_u.py": (
            "from pkg.use import Service\n\n\n"
            "def test_timeout():\n    assert Service().timeout() == 1\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/conf.py",
        "class Conf:\n    timeout = 2\n",
        {"tests/test_u.py::test_timeout"},
    )


def test_singledispatch_annotation(project: Project) -> None:
    source = (
        "import functools\n\n\n"
        "@functools.singledispatch\n"
        "def kind(x):\n    return 'other'\n\n\n"
        "@kind.register\n"
        "def _(x: int):\n    return 'int'\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/k.py": source,
        "tests/test_k.py": (
            "from pkg.k import kind\n\n\n"
            "def test_str():\n    assert kind('a') == 'other'\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/k.py",
        source.replace("x: int", "x: str"),
        {"tests/test_k.py::test_str"},
    )


def test_value_computation_raises_at_import(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conf.py": "CONFIG = {'timeout': 1}\n",
        "pkg/use.py": (
            "from pkg.conf import CONFIG\n\nTIMEOUT = CONFIG['timeout']\n\n\n"
            "def other():\n    return 1\n"
        ),
        "tests/test_u.py": (
            "from pkg.use import other\n\n\n"
            "def test_use():\n    assert other() == 1\n" + OTHER
        ),
    }
    check_fails(project, files, "pkg/conf.py", "CONFIG = {'limit': 1}\n")


def test_circular_import_reordered(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "X = 1\nfrom pkg import b\n\n\ndef g():\n    return 1\n",
        "pkg/b.py": "from pkg.a import X\n",
        "tests/test_a.py": (
            "from pkg import a\n\n\ndef test_g():\n    assert a.g() == 1\n" + OTHER
        ),
    }
    new = "from pkg import b\nX = 1\n\n\ndef g():\n    return 1\n"
    check_fails(project, files, "pkg/a.py", new)


def test_mapping_protocol_method_added(project: Project) -> None:
    source = "class Pairs:\n    def __iter__(self):\n        return iter([('a', 1)])\n"
    files = {
        "pkg/__init__.py": "",
        "pkg/p.py": source,
        "tests/test_p.py": (
            "from pkg.p import Pairs\n\n\n"
            "def test_dict():\n    assert dict(Pairs()) == {'a': 1}\n" + OTHER
        ),
    }
    check(
        project,
        files,
        "pkg/p.py",
        source + "\n    def keys(self):\n        return ['b']\n",
        {"tests/test_p.py::test_dict"},
    )


def test_file_existence_checked(project: Project) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/flags.py": (
            "import os\n\n\ndef enabled():\n    return os.path.exists('flags/beta')\n"
        ),
        "flags/README": "flags\n",
        "tests/test_f.py": (
            "from pkg.flags import enabled\n\n\n"
            "def test_enabled():\n    assert not enabled()\n" + OTHER
        ),
    }
    check(project, files, "flags/beta", "", {"tests/test_f.py::test_enabled"})


def test_symlinked_module(project: Project) -> None:
    files = {
        "shared/__init__.py": "",
        "shared/impl.py": "def value():\n    return 1\n",
        "pkg/__init__.py": "",
        "tests/test_s.py": (
            "from pkg.impl import value\n\n\n"
            "def test_value():\n    assert value() == 1\n" + OTHER
        ),
    }
    project.write(files)
    (project.path / "pkg" / "impl.py").symlink_to("../shared/impl.py")
    check(
        project,
        files,
        "shared/impl.py",
        "def value():\n    return 2\n",
        {"tests/test_s.py::test_value"},
    )
