"""The mypy plugin (``hare.contrib.mypy``), run by mypy itself on the case files: each line marked
``# E: <text>`` gets exactly one error holding the text, no other line gets one, and every ``assert_type``
holds."""

import re
import subprocess
import sys
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from mypy.options import Options

from hare.contrib.mypy.hare_mypy_plugin import HareMypyPlugin
from hare.contrib.mypy.model_registry import ModelRegistry
from hare.core.registries import Registries
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from tests.contrib.mypy.models import RatingField

pytestmark = pytest.mark.database_independent

CASES_PATH = Path(__file__).parent / "cases"
#: The repository root - where mypy runs, so the plugin imports the test modules by their names.
REPOSITORY_PATH = Path(__file__).resolve().parents[3]
CASE_FILES = sorted(path.name for path in CASES_PATH.glob("*.py"))
#: The case files of the PostgreSQL models, checked in a run of their own - by their path in the cases.
POSTGRESQL_CASE_FILES = sorted(f"postgresql/{path.name}" for path in (CASES_PATH / "postgresql").glob("*.py"))
MYPY_PYTHON_VERSION = "3.12"
MYPY_PROJECT_CONFIG = """
[tool.mypy]
python_version = "{python_version}"
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
    "postgresql_cases": (POSTGRESQL_CASE_FILES, "tests.contrib.mypy.postgresql_hare_config.HARE_CONFIG", "[]"),
    "unloadable_models": (
        ["field_names.py", "custom_lookups.py"],
        "tests.contrib.mypy.no_such_module.HARE_CONFIG",
        "[]",
    ),
}


def get_project_config(hare_orm: str, mypy_imports: str) -> str:
    return MYPY_PROJECT_CONFIG.format(python_version=MYPY_PYTHON_VERSION, hare_orm=hare_orm, mypy_imports=mypy_imports)


def start_mypy(project_path: Path, file_names: list[str], hare_orm: str, mypy_imports: str) -> subprocess.Popen[str]:
    """Start mypy with a private configuration and cache for this test run.

    Returns:
        The process.
    """
    project_path.mkdir(parents=True, exist_ok=True)
    config_path = project_path / "pyproject.toml"
    config_path.write_text(get_project_config(hare_orm, mypy_imports), encoding="utf-8")
    cache_path = project_path / ".mypy_cache"
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config_path),
            "--cache-dir",
            str(cache_path),
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
        file_name = Path(match["path"]).resolve().relative_to(CASES_PATH.resolve()).as_posix()
        messages[(file_name, int(match["line"]))].append(match["message"])
    assert process.returncode == int(bool(messages)), (process.returncode, stdout, stderr)
    return messages


def stop_mypy(process: subprocess.Popen[str]) -> None:
    """Stop an unfinished checker before its Popen context closes pipes and reaps it."""
    if process.poll() is None:
        process.kill()


@pytest.fixture(scope="module")
def mypy_errors(tmp_path_factory) -> dict[str, dict[tuple[str, int], list[str]]]:
    """Run independent checkers concurrently with caches and processes scoped to this run.

    Every started process is killed and reaped on setup, parsing or assertion failure;
    no shared temporary directories are removed.
    """
    projects_path = tmp_path_factory.mktemp("mypy-plugin")
    with ExitStack() as cleanup:
        processes = {}
        for name, arguments in MYPY_RUNS.items():
            process = cleanup.enter_context(start_mypy(projects_path / name, *arguments))
            cleanup.callback(stop_mypy, process)
            processes[name] = process
        results = {name: read_mypy_errors(process) for name, process in processes.items()}
        for name, errors in results.items():
            expected_files = set(MYPY_RUNS[name][0])
            assert {file_name for file_name, _ in errors} <= expected_files, errors
        return results


@pytest.mark.parametrize(
    ("run_name", "file_name"),
    [
        *(("cases", name) for name in CASE_FILES),
        *(("postgresql_cases", name) for name in POSTGRESQL_CASE_FILES),
    ],
)
def test_a_case_file_gets_exactly_its_expected_errors(mypy_errors, run_name, file_name):
    expected_by_line = {
        line_number: match["message"]
        for line_number, line in enumerate((CASES_PATH / file_name).read_text(encoding="utf-8").splitlines(), start=1)
        if (match := EXPECTED_ERROR.search(line)) is not None
    }
    actual_by_line = {line: messages for (name, line), messages in mypy_errors[run_name].items() if name == file_name}
    assert actual_by_line.keys() == expected_by_line.keys(), (expected_by_line, actual_by_line)
    for line, expected in expected_by_line.items():
        messages = actual_by_line[line]
        assert len(messages) == 1 and expected in messages[0], (line, expected, messages)


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


def test_the_fingerprint_follows_the_registered_lookups(tmp_path):
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        get_project_config(hare_orm="tests.contrib.mypy.hare_config.HARE_CONFIG", mypy_imports="[]"), encoding="utf-8"
    )
    options = Options()
    options.config_file = str(config_path)
    registry = ModelRegistry(options)
    registry.load()
    assert registry.load_error is None
    assert registry.get_model("tests.contrib.mypy.models.Writer") is not None
    assert registry.get_model("tests.contrib.mypy.models.Missing") is None
    previous_snapshot = Registries.snapshot
    RatingField.register_lookup("rated_for_fingerprint", lambda field: FieldLookup(Lookups.between))
    try:
        changed_registry = ModelRegistry(options)
        changed_registry.load()
        assert changed_registry.load_error is None
        assert changed_registry.fingerprint != registry.fingerprint
        RatingField.register_lookup(
            "rated_for_fingerprint", lambda field: FieldLookup(Lookups.between), value_type=str
        )
        replaced_registry = ModelRegistry(options)
        replaced_registry.load()
        assert replaced_registry.load_error is None
        assert replaced_registry.fingerprint != changed_registry.fingerprint
    finally:
        Registries.snapshot = previous_snapshot
    plugin = HareMypyPlugin(options)
    assert plugin.report_config_data(SimpleNamespace(id="any", path="any.py", is_check=True)) == registry.fingerprint
    assert HareMypyPlugin.for_version("1.19.1") is HareMypyPlugin
