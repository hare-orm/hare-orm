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
from tests.matrix.matrix_runner import MatrixRunner
from tests.matrix.native_interpreters import NativeInterpreters
from tests.matrix.paths import REPOSITORY_DIRECTORY
from tests.matrix.suite_environments import SuiteEnvironments
from tests.matrix.suite_requirements import SuiteRequirements

pytestmark = pytest.mark.database_independent


def test_python_version_selects_supported_extras_and_database_drivers():
    older = {environment.name: environment for environment in MatrixCatalog.get_environments("3.14")}
    newer = {environment.name: environment for environment in MatrixCatalog.get_environments("3.15")}
    assert {"robyn", "clickhouse-driver"} <= older.keys()
    assert not {"robyn", "clickhouse-driver"} & newer.keys()
    assert set(older["all"].extras) - set(newer["all"].extras) == {"robyn", "clickhouse-driver"}
    assert "clickhouse+clickhouse-driver" not in newer["all"].databases
    assert "clickhouse+clickhouse-connect" in newer["all"].databases
    assert {"asyncpg", "pydantic", "fastapi", "litestar", "kafka"} <= set(newer["all"].extras)


def test_clickhouse_ci_preserves_every_server_on_each_drivers_latest_python():
    rows = MatrixCatalog.get_ci_matrices()["test-clickhouse"]["include"]
    for server in MatrixCatalog.get("versions.clickhouse"):
        assert {"clickhouse": server, "driver": "clickhouse-connect", "python": "3.15"} in rows
        assert {"clickhouse": server, "driver": "clickhouse-driver", "python": "3.14"} in rows
    assert not any(row["driver"] == "clickhouse-driver" and row["python"] == "3.15" for row in rows)


def test_core_databases_follow_their_latest_compatible_python():
    runs = MatrixRunner(["3.14", "3.15"], [], ["core"], "1", False, True).get_runs()
    for database, version in (
        ("clickhouse+clickhouse-driver", "3.14"),
        ("clickhouse+clickhouse-connect", "3.15"),
        ("postgresql", "3.15"),
    ):
        selected = [run for run in runs if run.database == database]
        assert selected
        assert {run.python_version for run in selected} == {version}


def test_an_unavailable_environment_is_refused_instead_of_reporting_an_empty_success():
    with pytest.raises(SystemExit, match="No supported test environments"):
        MatrixRunner(["3.15"], [], ["robyn"], "1", False, True).get_runs()


def test_aggregate_collection_omits_only_unavailable_integrations(monkeypatch):
    environment = next(
        environment for environment in MatrixCatalog.get_environments("3.15") if environment.name == "all"
    )
    monkeypatch.setattr(
        SuiteEnvironments,
        "current_installed",
        SuiteRequirements.get_installed(SuiteRequirements.get_requirements(environment)),
    )
    monkeypatch.setattr(SuiteEnvironments, "runs_every_environment", True)
    robyn_path = REPOSITORY_DIRECTORY / "tests/contrib/frameworks/robyn/test_request_state.py"
    assert SuiteEnvironments.is_other_environment_path(robyn_path)
    assert not SuiteEnvironments.is_other_environment_path(
        REPOSITORY_DIRECTORY / "tests/native_acceleration/test_field_codecs_asyncpg.py"
    )
    item = SimpleNamespace(
        path=robyn_path, nodeid="unavailable integration", add_marker=lambda marker: None, iter_markers=lambda name: ()
    )
    assert SuiteEnvironments.get_other_environment_items([item]) == [item]


def test_installer_options_can_be_read_without_site_packages():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "tests.matrix.matrix_catalog",
            "extras",
            "--python-version",
            "3.15",
            "--prefix=--extra=",
            "--json",
        ],
        cwd=REPOSITORY_DIRECTORY,
        capture_output=True,
        text=True,
        check=True,
    )
    extras = json.loads(result.stdout)
    assert "--extra=asyncpg" in extras
    assert "--extra=robyn" not in extras
    assert "--extra=clickhouse-driver" not in extras


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
