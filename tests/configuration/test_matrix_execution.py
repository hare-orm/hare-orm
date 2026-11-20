from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.matrix import matrix_runner
from tests.matrix.constants import RUN_REPORT_ENVIRONMENT_VARIABLE
from tests.matrix.contract_coverage import ContractCoverage
from tests.matrix.matrix_runner import MatrixRunner
from tests.matrix.matrix_virtualenv import MatrixVirtualenv
from tests.matrix.process_groups import ProcessGroups
from tests.matrix.run_report import RunReport
from tests.matrix.run_result import RunResult
from tests.matrix.run_scheduler import RunScheduler
from tests.matrix.run_spec import RunSpec
from tests.matrix.suite_environment import SuiteEnvironment

pytestmark = pytest.mark.database_independent


@pytest.fixture
def run_spec():
    return RunSpec(
        "3.14",
        "profile",
        SuiteEnvironment("core", whole_suite=True),
        "postgresql",
        {"HARE_TEST_DB": "postgresql://localhost/test_{}"},
        resources=frozenset({"postgresql:18"}),
    )


@pytest.mark.parametrize(("exit_code", "passed"), [(0, True), (5, False), (1, False)])
def test_process_status_and_report_details_are_preserved(tmp_path, run_spec, exit_code, passed):
    log = tmp_path / "run.log"
    log.with_suffix(".json").write_text(
        json.dumps(
            {
                "exit_code": exit_code,
                "counts": {},
                "collected": 0,
                "seed": 321,
                "tool_versions": {"pytest": "test-version"},
            }
        )
    )
    result = RunResult.read(run_spec, exit_code, 0.25, log)
    assert result.exit_code == exit_code
    assert result.passed is passed
    assert result.seed == 321
    assert result.tool_versions == {"pytest": "test-version"}


@pytest.mark.parametrize("missing", ["exit_code", "counts", "collected", "seed", "tool_versions"])
def test_zero_exit_does_not_hide_an_incomplete_report(tmp_path, run_spec, missing):
    report = {"exit_code": 0, "counts": {"passed": 3}, "collected": 3, "seed": 321, "tool_versions": {}}
    del report[missing]
    log = tmp_path / "run.log"
    log.with_suffix(".json").write_text(json.dumps(report))
    result = RunResult.read(run_spec, 0, 1, log)
    assert not result.passed
    assert result.report_error


def test_report_uses_controller_outcomes_seed_and_actual_tool_versions(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    monkeypatch.setenv(RUN_REPORT_ENVIRONMENT_VARIABLE, str(target))
    selector = "tests/example.py::test_contract"
    monkeypatch.setattr(ContractCoverage, "get_selectors", lambda: {selector: ["rust_pg-*"]})
    passed = SimpleNamespace(nodeid=f"{selector}[ok]")
    broken = SimpleNamespace(nodeid=f"{selector}[teardown]")
    skipped = SimpleNamespace(nodeid=f"{selector}[skip]", longrepr=("example.py", 1, "Skipped: unavailable"))
    excluded = SimpleNamespace(nodeid="tests/example.py::test_other")
    reporter = SimpleNamespace(
        stats={"passed": [passed, broken], "error": [broken], "skipped": [skipped], "deselected": [excluded]}
    )
    config = SimpleNamespace(
        pluginmanager=SimpleNamespace(getplugin=lambda name: reporter), getoption=lambda name, default=None: 321
    )
    session = SimpleNamespace(config=config, exitstatus=1, testscollected=3)
    RunReport.write(session)
    report = json.loads(target.read_text())
    assert report["counts"] == {"passed": 2, "errors": 1, "skipped": 1, "deselected": 1}
    assert report["contracts"] == {
        passed.nodeid: {"status": "passed"},
        broken.nodeid: {"status": "error"},
        skipped.nodeid: {"status": "skipped", "reason": "Skipped: unavailable"},
    }
    assert report["seed"] == 321
    assert report["tool_versions"]["pytest"] == pytest.__version__
    config.workerinput = {}
    session.exitstatus = 0
    RunReport.write(session)
    assert json.loads(target.read_text())["exit_code"] == 1


@pytest.mark.parametrize("status", [None, "skipped", "deselected", "xfailed", "passed"])
def test_required_case_must_pass_on_its_declared_profile(tmp_path, monkeypatch, run_spec, status):
    family = "tests/example.py::test_contract"
    required = f"{family}[composite]"
    monkeypatch.setattr(ContractCoverage, "get_selectors", lambda: {required: ["rust_pg-*"]})
    log = tmp_path / "run.log"
    outcomes = {f"{family}[scalar]": {"status": "passed"}}
    if status is not None:
        outcomes[required] = {"status": status}
    log.with_suffix(".json").write_text(
        json.dumps(
            {"exit_code": 0, "counts": {}, "collected": 2, "seed": 321, "tool_versions": {}, "contracts": outcomes}
        )
    )
    all_extras = SuiteEnvironment("all", whole_suite=True)
    required_profile = replace(run_spec, suite="rust_pg-18", suite_environment=all_extras)
    result = RunResult.read(required_profile, 0, 1, log)
    assert result.passed is (status == "passed")
    if status != "passed":
        assert required in result.report_error
    assert RunResult.read(replace(required_profile, suite="asyncpg-18"), 0, 1, log).passed
    assert RunResult.read(replace(required_profile, suite_environment=run_spec.suite_environment), 0, 1, log).passed


def test_scheduler_serializes_shared_resources_and_keeps_independent_runs_parallel(run_spec, tmp_path):
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = set()
    specs = [
        replace(run_spec, suite="first"),
        replace(run_spec, suite="second"),
        replace(run_spec, suite="other", resources=frozenset({"postgresql:14"})),
    ]

    def execute(spec):
        with lock:
            assert not active.intersection(spec.resources)
            active.update(spec.resources)
        try:
            if spec.suite != "second":
                barrier.wait(timeout=10)
            return RunResult(spec, 0, {"passed": 1}, 0, tmp_path / "run.log")
        finally:
            with lock:
                active.difference_update(spec.resources)

    results = RunScheduler(2, 1).run(specs, execute, barrier.abort, lambda *args: None)
    assert [result.spec for result in results] == specs
    assert not active


def test_scheduler_stops_active_work_before_propagating_failure(run_spec, tmp_path):
    barrier = threading.Barrier(2)
    stopped, finished = threading.Event(), threading.Event()
    specs = [
        replace(run_spec, suite="fail", resources=frozenset()),
        replace(run_spec, suite="wait", resources=frozenset()),
    ]

    def execute(spec):
        barrier.wait(timeout=10)
        if spec.suite == "fail":
            raise RuntimeError("launch failed")
        assert stopped.wait(timeout=10)
        finished.set()
        return RunResult(spec, 0, {}, 0, tmp_path / "run.log")

    with pytest.raises(RuntimeError, match="launch failed"):
        RunScheduler(2, 1).run(specs, execute, stopped.set, lambda *args: None)
    assert finished.is_set()


@pytest.mark.parametrize("sqlite_version", [None, "3.53.4"])
def test_profile_execution_clears_foreign_test_environment_and_keeps_exit_five(
    run_spec, tmp_path, monkeypatch, sqlite_version
):
    run_spec = replace(run_spec, sqlite_version=sqlite_version)
    loaded_versions = []

    def load_variables(selection):
        loaded_versions.append((selection.version, selection.python_version))
        return {"HARE_TEST_SQLITE_VERSION": selection.version}

    monkeypatch.setattr(matrix_runner.SqliteVersionEnvironment, "get_variables", load_variables)
    monkeypatch.setenv("HARE_TEST_CLICKHOUSE_CLUSTER_DB", "stale")
    monkeypatch.setenv("HARE_TEST_DB_UNTAGGED", "stale")
    monkeypatch.setenv("MATRIX_PROBE", "keep")
    monkeypatch.setattr(matrix_runner, "MATRIX_LOG_DIRECTORY", tmp_path)
    monkeypatch.setattr(
        MatrixVirtualenv,
        "get",
        lambda _python_version, _environment, _database: SimpleNamespace(
            get_python_executable=lambda: Path(sys.executable)
        ),
    )
    runner = MatrixRunner([], [], [], "1", False, False)

    def execute(command, *, cwd, environment, output):
        assert environment["HARE_TEST_DB"] == run_spec.environment["HARE_TEST_DB"]
        assert "HARE_TEST_DB_UNTAGGED" not in environment
        assert "HARE_TEST_CLICKHOUSE_CLUSTER_DB" not in environment
        assert environment["MATRIX_PROBE"] == "keep"
        assert "tests.matrix.run_report" in command
        assert environment.get("HARE_TEST_SQLITE_VERSION") == sqlite_version
        Path(environment[RUN_REPORT_ENVIRONMENT_VARIABLE]).write_text(
            json.dumps({"exit_code": 5, "counts": {}, "collected": 0, "seed": 321, "tool_versions": {}})
        )
        return 5

    processes = ProcessGroups()
    monkeypatch.setattr(processes, "run", execute)
    result = runner.execute(run_spec, processes=processes)
    assert result.exit_code == 5
    assert not result.passed
    assert loaded_versions == ([] if sqlite_version is None else [(sqlite_version, run_spec.python_version)])


def test_selecting_profiles_does_not_require_sqlite_artifacts(monkeypatch):
    def refuse_artifact_read(selection):
        pytest.fail(f"Unexpected artifact read for unexecuted SQLite {selection.version}")

    monkeypatch.setattr(matrix_runner.SqliteVersionEnvironment, "get_variables", refuse_artifact_read)
    runner = MatrixRunner(["3.14", "3.14"], ["database-independent"], [], "1", False, False)
    runs = runner.get_runs()
    assert runs
    assert all(run.suite == "database-independent" and run.sqlite_version is None for run in runs)
    assert len(runs) == len({run.suite_environment.name for run in runs})
