"""The mypy plugin (``hare.contrib.mypy``), run by mypy itself on the case files: each line marked
``# E: <text>`` gets an error holding the text, no other line gets one, and every ``assert_type``
holds."""

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from mypy.options import Options

from hare.contrib.mypy.hare_mypy_plugin import HareMypyPlugin
from hare.contrib.mypy.model_registry import ModelRegistry
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from tests.contrib.mypy.models import RatingField

pytestmark = pytest.mark.database_independent

CASES_PATH = Path(__file__).parent / "cases"
#: The repository root - where mypy runs, so the plugin imports the test modules by their names.
REPOSITORY_PATH = Path(__file__).resolve().parents[3]
CASE_FILES = sorted(path.name for path in CASES_PATH.glob("*.py"))
MYPY_PROJECT_CONFIG = """
[tool.mypy]
python_version = "3.12"
plugins = ["hare.contrib.mypy"]
ignore_missing_imports = true
show_error_codes = true

[[tool.mypy.overrides]]
module = ["hare.*", "rust.*"]
follow_imports = "silent"

[tool.hare]
hare_orm = "{hare_orm}"
mypy_imports = {mypy_imports}
"""
EXPECTED_ERROR = re.compile(r"  # E: (?P<message>.+?)\s*$")
MYPY_MESSAGE = re.compile(r"^(?P<path>.+?\.py):(?P<line>\d+): error: (?P<message>.*?)(?:  \[[\w-]+\])?$")


#: The mypy runs of the module by name: the case files checked, ``hare_orm`` and ``mypy_imports`` of
#: the configuration.
MYPY_RUNS = {
    "cases": (CASE_FILES, "tests.contrib.mypy.hare_config.HARE_CONFIG", '["tests.contrib.mypy.lookups"]'),
    "unloadable_models": (
        ["field_names.py", "custom_lookups.py"],
        "tests.contrib.mypy.no_such_module.HARE_CONFIG",
        "[]",
    ),
    "invalid_imports": (
        ["custom_lookups.py"],
        "tests.contrib.mypy.hare_config.HARE_CONFIG",
        '"tests.contrib.mypy.lookups"',
    ),
}


def start_mypy(project_path: Path, file_names: list[str], hare_orm: str, mypy_imports: str) -> subprocess.Popen[str]:
    """Starts mypy with the plugin on case files, in a process of its own.

    Returns:
        The process.
    """
    config_path = project_path / "pyproject.toml"
    config_path.write_text(MYPY_PROJECT_CONFIG.format(hare_orm=hare_orm, mypy_imports=mypy_imports), encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config_path),
            "--cache-dir",
            str(project_path / ".mypy_cache"),
            "--no-pretty",
            "--hide-error-context",
            "--no-error-summary",
            *(str(CASES_PATH / name) for name in file_names),
        ],
        cwd=REPOSITORY_PATH,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def read_mypy_errors(process: subprocess.Popen[str]) -> dict[tuple[str, int], list[str]]:
    """Waits for a mypy run.

    Returns:
        Its error messages by file name and line.
    """
    stdout, stderr = process.communicate()
    assert not stderr, stderr
    messages: dict[tuple[str, int], list[str]] = defaultdict(list)
    for output_line in stdout.splitlines():
        match = MYPY_MESSAGE.match(output_line)
        assert match is not None, output_line
        messages[(Path(match["path"]).name, int(match["line"]))].append(match["message"])
    return messages


@pytest.fixture(scope="module")
def mypy_errors(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[tuple[str, int], list[str]]]:
    """The errors of every mypy run of the module, by the run's name. A run checks hare itself and
    takes ten seconds, so they go side by side."""
    processes = {name: start_mypy(tmp_path_factory.mktemp(name), *arguments) for name, arguments in MYPY_RUNS.items()}
    return {name: read_mypy_errors(process) for name, process in processes.items()}


@pytest.mark.parametrize("file_name", CASE_FILES)
def test_a_case_file_gets_exactly_its_expected_errors(mypy_errors, file_name):
    expected_by_line = {
        line_number: match["message"]
        for line_number, line in enumerate((CASES_PATH / file_name).read_text(encoding="utf-8").splitlines(), start=1)
        if (match := EXPECTED_ERROR.search(line)) is not None
    }
    actual_by_line = {line: messages for (name, line), messages in mypy_errors["cases"].items() if name == file_name}
    missing = {
        line: expected
        for line, expected in expected_by_line.items()
        if not any(expected in message for message in actual_by_line.get(line, []))
    }
    unexpected = {line: messages for line, messages in actual_by_line.items() if line not in expected_by_line}
    assert not missing, f"expected errors not reported: {missing}\nreported: {actual_by_line}"
    assert not unexpected, f"errors not expected: {unexpected}"


def test_models_that_cant_be_loaded_are_reported_once_in_each_module(mypy_errors):
    errors = mypy_errors["unloadable_models"]
    load_errors = {
        file_name: [message for message in messages if "the models can't be loaded" in message]
        for (file_name, _), messages in errors.items()
    }
    assert sorted(file_name for file_name, messages in load_errors.items() if messages) == [
        "custom_lookups.py",
        "field_names.py",
    ]
    assert all(len(messages) == 1 for messages in load_errors.values() if messages)
    assert "Cannot import configuration module 'tests.contrib.mypy.no_such_module'" in next(iter(errors.values()))[0]


def test_mypy_imports_must_be_a_list_of_module_names(mypy_errors):
    errors = mypy_errors["invalid_imports"]
    assert len(errors) == 1
    assert "mypy_imports in" in next(iter(errors.values()))[0]
    assert "must be a list of module names" in next(iter(errors.values()))[0]


def test_the_fingerprint_follows_the_registered_lookups(tmp_path):
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        MYPY_PROJECT_CONFIG.format(hare_orm="tests.contrib.mypy.hare_config.HARE_CONFIG", mypy_imports="[]"),
        encoding="utf-8",
    )
    options = Options()
    options.config_file = str(config_path)
    registry = ModelRegistry(options)
    registry.load()
    assert registry.load_error is None
    assert registry.get_model("tests.contrib.mypy.models.Writer") is not None
    assert registry.get_model("tests.contrib.mypy.models.Missing") is None
    RatingField.register_lookup("rated_for_fingerprint", lambda field: FieldLookup(Lookups.between))
    try:
        changed_registry = ModelRegistry(options)
        changed_registry.load()
        assert changed_registry.fingerprint != registry.fingerprint
    finally:
        del RatingField.registered_lookups["rated_for_fingerprint"]
    plugin = HareMypyPlugin(options)
    assert plugin.report_config_data(SimpleNamespace(id="any", path="any.py", is_check=True)) == registry.fingerprint
    assert HareMypyPlugin.for_version("1.19.1") is HareMypyPlugin
