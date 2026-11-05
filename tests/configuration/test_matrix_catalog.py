from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.lowest_dependencies.locked_tool_requirements import LockedToolRequirements
from tests.matrix.ci_results import CIResults
from tests.matrix.matrix_catalog import MatrixCatalog
from tests.matrix.native_interpreters import NativeInterpreters
from tests.matrix.paths import REPOSITORY_DIRECTORY

pytestmark = pytest.mark.database_independent


def test_locked_tools_keep_every_platform_alternative_and_exclude_project_dependencies(tmp_path, monkeypatch):
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\ndependencies = ["shared>=1"]\n'
        '[project.optional-dependencies]\nhttp = ["shared-extra>=1"]\n'
        "[tool.poetry.group.test.dependencies]\n"
        'my_tool = "*"\nwin-tool = "*"\nshared = "*"\nshared-extra = "*"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(LockedToolRequirements, "get_tool_groups", staticmethod(lambda: ["test"]))
    tools = LockedToolRequirements(project)
    alternatives = [
        'my-tool==1.0; python_version < "3.13"',
        'my-tool==2.0; python_version >= "3.13"',
        'win-tool==3.0; sys_platform == "win32"',
    ]
    exported = "\n".join(
        [
            "# exported requirements",
            "--extra-index-url https://example.invalid/simple",
            *alternatives,
            "shared==9.0",
            "Shared.Extra==8.0",
            "transitive-tool==4.0",
        ]
    )
    assert tools.select_tools(exported) == ["--extra-index-url https://example.invalid/simple", *alternatives]
    with pytest.raises(SystemExit, match="win-tool"):
        tools.select_tools("\n".join(alternatives[:2]))


def test_catalog_cli_needs_only_the_standard_library():
    result = subprocess.run(
        [sys.executable, "-S", "-m", "tests.matrix.matrix_catalog", "ci", "--json"],
        cwd=REPOSITORY_DIRECTORY,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == MatrixCatalog.get_ci_matrices()


def test_native_interpreters_follow_catalog_order_and_refuse_missing_versions(monkeypatch):
    versions = ["3.13", "3.12"]
    paths = {version: SimpleNamespace(executable=Path(f"python-{version}")) for version in versions}
    monkeypatch.setattr(MatrixCatalog, "get", lambda path: versions)
    monkeypatch.setitem(
        sys.modules,
        "findpython",
        SimpleNamespace(Finder=lambda **options: SimpleNamespace(find=lambda version, **options: paths.get(version))),
    )
    assert NativeInterpreters.get_paths() == [Path("python-3.13"), Path("python-3.12")]
    versions.append("3.14")
    with pytest.raises(RuntimeError, match=r"CPython 3\.14.*missing"):
        NativeInterpreters.get_paths()


@pytest.mark.parametrize("status", ["skipped", "failure", "cancelled", None])
def test_regular_ci_rejects_every_unsuccessful_job(status):
    jobs = {"catalog": {"result": "success"}, "test": {"result": status}}
    assert CIResults.get_errors(jobs, ["catalog", "test"], skip_tests=False)


def test_reduced_ci_requires_exactly_the_intentional_skips():
    jobs = {"catalog": {"result": "success"}, "test": {"result": "skipped"}}
    assert not CIResults.get_errors(jobs, ["catalog", "test"], skip_tests=True)
    jobs["test"]["result"] = "failure"
    assert CIResults.get_errors(jobs, ["catalog", "test"], skip_tests=True)
    jobs["test"]["result"] = "skipped"
    jobs["catalog"]["result"] = "skipped"
    assert CIResults.get_errors(jobs, ["catalog", "test"], skip_tests=True)


@pytest.mark.parametrize("expected", [["catalog", "test", "missing"], ["catalog"]])
def test_ci_job_inventory_cannot_silently_change(expected):
    assert CIResults.get_errors(
        {"catalog": {"result": "success"}, "test": {"result": "success"}}, expected, skip_tests=False
    )
