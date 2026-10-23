"""Added files change their own directory's listing, and directories they create."""

from tests.conftest import Project

TESTS = {
    "tests/test_check.py": (
        "import os\n\n\ndef test_check():\n    assert os.path.isdir('data/deep')\n"
    ),
    "tests/test_root.py": (
        "import os\n\n\ndef test_root():\n    assert 'data' in os.listdir('.')\n"
    ),
    "tests/test_deep.py": (
        "import os\n\n\ndef test_deep():\n"
        "    assert os.listdir('data/deep') == ['a.txt']\n"
    ),
}


def test_file_added_to_an_existing_directory(project: Project) -> None:
    project.commit({"data/deep/a.txt": "", **TESTS})
    project.run("--blast-radius").assert_outcomes(passed=3)
    project.write({"data/deep/b.txt": ""})
    assert project.ran("--blast-radius") == {"tests/test_deep.py::test_deep"}


def test_file_removed_from_a_directory_that_stays(project: Project) -> None:
    deep = TESTS["tests/test_deep.py"].replace("== ['a.txt']", "!= []")
    project.commit(
        {
            "data/deep/a.txt": "",
            "data/deep/b.txt": "",
            **TESTS,
            "tests/test_deep.py": deep,
        }
    )
    project.run("--blast-radius").assert_outcomes(passed=3)
    (project.path / "data/deep/b.txt").unlink()
    assert project.ran("--blast-radius") == {"tests/test_deep.py::test_deep"}


def test_file_added_in_a_new_directory(project: Project) -> None:
    project.commit({"data/deep/a.txt": "", **TESTS})
    project.run("--blast-radius").assert_outcomes(passed=3)
    project.write({"data/new/b.txt": ""})
    # The listing of data/ changes; nothing listed it, and data/deep still exists.
    assert project.ran("--blast-radius") == set()
    project.write({"data/deep/sub/c.txt": ""})
    assert project.ran("--blast-radius") == {"tests/test_deep.py::test_deep"}
