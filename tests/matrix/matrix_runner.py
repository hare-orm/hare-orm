"""Runs the tests of every environment on every Python, SQLite, PostgreSQL, PgBouncer, ClickHouse and broker
version of the matrix and prints a table of the outcomes. The tests reading no test database are a
suite of their own, run once per Python and environment. Each run has the virtualenv of its environment and
of its database's driver (``MatrixVirtualenv``): ``--virtualenvs`` creates those of the runs chosen.

Needs the SQLite versions (``tests/sqlite_versions/download_sqlite_versions.py`` run by each Python) and the
servers of the suites run: ``make test_db_matrix_up``, ``make test_pgbouncer_matrix_up``, ``make
test_clickhouse_matrix_up``, ``make test_clickhouse_cluster_matrix_up``, ``make test_brokers_matrix_up``.

Usage:
    python -m tests.matrix.matrix_runner [--python 3.12 ...] [--suite sqlite-3.35.5 ...]
        [--environment core ...] [--workers 8] [--jobs 1] [--print-failed-logs] [--newest]
        [--virtualenvs [--interpreter PATH]]
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys
import time
from pathlib import Path

from tests.matrix.constants import (
    DATABASE_INDEPENDENT_MARKER,
    DATABASE_INDEPENDENT_SUITE,
    FEW_TEST_RUNS_AT_ONCE,
    MATRIX_BROKERS,
    MATRIX_CLICKHOUSE_CLUSTER_TEST_PATHS,
    MATRIX_CLICKHOUSE_CLUSTER_URL_QUERY,
    MATRIX_CLICKHOUSE_TEST_PATHS,
    MATRIX_CLICKHOUSE_URL_QUERY,
    MATRIX_CLICKHOUSE_URLS,
    MATRIX_CLICKHOUSE_VERSIONS,
    MATRIX_DATABASES_BY_DRIVER,
    MATRIX_PGBOUNCER_URL_QUERY,
    MATRIX_PGBOUNCER_VERSIONS,
    MATRIX_POSTGRES_URLS,
    MATRIX_POSTGRES_VERSIONS,
    MATRIX_PYTEST_OPTIONS,
    MATRIX_PYTHON_VERSIONS,
    RUN_REPORT_ENVIRONMENT_VARIABLE,
    SQLITE_REGEXP_TEST_PATHS,
    SQLITE_SUITE_PREFIX,
    SUITE_ENVIRONMENT_OPTION,
    SUITE_ENVIRONMENTS,
    WITHOUT_NATIVE_PLUGIN,
)
from tests.matrix.matrix_ports import MatrixPorts
from tests.matrix.matrix_virtualenv import MatrixVirtualenv
from tests.matrix.paths import MATRIX_LOG_DIRECTORY, REPOSITORY_DIRECTORY
from tests.matrix.process_groups import ProcessGroups
from tests.matrix.run_result import RunResult
from tests.matrix.run_scheduler import RunScheduler
from tests.matrix.run_spec import RunSpec
from tests.matrix.suite_environment import SuiteEnvironment
from tests.matrix.suite_requirements import SuiteRequirements
from tests.sqlite_versions.constants import SQLITE_RELEASES
from tests.sqlite_versions.environment import SqliteVersionEnvironment


class MatrixRunner:
    """Runs the test matrix with bounded concurrency and exclusive shared resources, then reports it."""

    def __init__(
        self,
        python_versions: list[str],
        suites: list[str],
        environments: list[str],
        workers: str,
        print_failed_logs: bool,
        newest: bool,
        jobs: int = 1,
    ) -> None:
        """
        Args:
            python_versions: The Python versions to run on, every one of the matrix when empty -
                the newest one with ``newest``.
            suites: The suites to run (``sqlite-3.35.5``, ``asyncpg-14``, ``pgbouncer-rust_pg-1.21-14``,
                ``clickhouse-driver-26.3``, ``clickhouse-connect-cluster-25.8``, ``redis-8.10``, ...) or
                shell-style patterns of them (``sqlite-*``), every one when empty.
            environments: The environments of ``SUITE_ENVIRONMENTS`` whose tests run, or patterns of their
                names, every one when empty.
            workers: pytest-xdist workers of each run - a number or ``auto``.
            print_failed_logs: Whether the pytest output of every failed run is printed after the
                table.
            newest: Whether only the newest version of each server of the matrix runs.
            jobs: How many runs may execute at once, each with ``workers`` pytest workers. Runs sharing
                a physical server are serialized; independent resources can run concurrently.

        Raises:
            SystemExit: A Python version, a suite or an environment isn't part of the matrix.
        """
        unknown_python_versions = sorted(set(python_versions) - set(MATRIX_PYTHON_VERSIONS))
        if unknown_python_versions:
            raise SystemExit(
                f"Unknown Python versions: {', '.join(unknown_python_versions)} - "
                f"known: {', '.join(MATRIX_PYTHON_VERSIONS)}"
            )
        environment_names = [environment.name for environment in SUITE_ENVIRONMENTS]
        unknown_environments = [pattern for pattern in environments if not fnmatch.filter(environment_names, pattern)]
        if unknown_environments:
            raise SystemExit(
                f"No environment matches {', '.join(unknown_environments)} - known: {', '.join(environment_names)}"
            )
        self.suite_environments = [
            environment
            for environment in SUITE_ENVIRONMENTS
            if not environments or any(fnmatch.fnmatchcase(environment.name, pattern) for pattern in environments)
        ]
        self.python_versions = python_versions or list(
            MATRIX_PYTHON_VERSIONS[-1:] if newest else MATRIX_PYTHON_VERSIONS
        )
        self.sqlite_versions = list(SQLITE_RELEASES)[-1:] if newest else list(SQLITE_RELEASES)
        self.postgres_versions = list(MATRIX_POSTGRES_VERSIONS[-1:] if newest else MATRIX_POSTGRES_VERSIONS)
        self.pgbouncer_versions = list(MATRIX_PGBOUNCER_VERSIONS[-1:] if newest else MATRIX_PGBOUNCER_VERSIONS)
        self.clickhouse_versions = list(MATRIX_CLICKHOUSE_VERSIONS[-1:] if newest else MATRIX_CLICKHOUSE_VERSIONS)
        self.broker_versions = {
            broker: list(versions[-1:] if newest else versions)
            for broker, (versions, _, _, _) in MATRIX_BROKERS.items()
        }
        self.suites = suites
        self.workers = workers
        if jobs < 1:
            raise SystemExit(f"--jobs is a number of runs at once, 1 or more - not {jobs}")
        self.jobs = jobs
        self.print_failed_logs = print_failed_logs
        self.processes = ProcessGroups()

    def get_runs(self) -> list[RunSpec]:
        """The runs of the matrix, filtered by the requested suites and environments.

        Raises:
            SystemExit: A requested suite isn't part of the matrix.
        """
        SuiteRequirements.manifest.check_inventory()
        runs: list[RunSpec] = []
        for python_version in self.python_versions:
            for suite_environment in self.suite_environments:
                runs.extend(self.get_environment_runs(python_version, suite_environment))
        if self.suites:
            known_suites = list(dict.fromkeys(run.suite for run in runs))
            unknown_suites = [pattern for pattern in self.suites if not fnmatch.filter(known_suites, pattern)]
            if unknown_suites:
                raise SystemExit(f"No suite matches {', '.join(unknown_suites)} - known: {', '.join(known_suites)}")
            runs = [run for run in runs if any(fnmatch.fnmatchcase(run.suite, pattern) for pattern in self.suites)]
        return runs

    def get_environment_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[RunSpec]:
        """The runs of an environment's tests on one Python: the tests reading no database, and the suite of
        each database the environment runs on - an environment of a broker's extra on that broker's servers.
        """
        # The tests reading no test database - once per Python and environment, not on every database; an
        # environment of extras collects only the modules that may hold its tests.
        test_paths = () if suite_environment.whole_suite else SuiteRequirements.get_test_paths(suite_environment)
        runs = (
            [
                RunSpec(
                    python_version,
                    DATABASE_INDEPENDENT_SUITE,
                    suite_environment,
                    "sqlite",
                    {"HARE_TEST_DB": "sqlite+aiosqlite://:memory:"},
                    test_paths,
                    marker_expression=DATABASE_INDEPENDENT_MARKER,
                )
            ]
            if suite_environment.database_independent and (suite_environment.whole_suite or test_paths)
            else []
        )
        if suite_environment.extras and set(suite_environment.extras) <= set(MATRIX_BROKERS):
            return runs + self.get_broker_runs(python_version, suite_environment)
        newest_only = suite_environment.on_newest_servers_only
        if newest_only and python_version != MATRIX_PYTHON_VERSIONS[-1]:
            return runs
        databases = suite_environment.databases
        if "sqlite" in databases:
            for sqlite_version in self.sqlite_versions[-1:] if newest_only else self.sqlite_versions:
                runs.append(
                    RunSpec(
                        python_version,
                        f"{SQLITE_SUITE_PREFIX}{sqlite_version}",
                        suite_environment,
                        "sqlite",
                        {"HARE_TEST_DB": "sqlite+aiosqlite://:memory:"},
                        sqlite_version=sqlite_version,
                    )
                )
            newest_sqlite_version = list(SQLITE_RELEASES)[-1]
            runs.append(
                RunSpec(
                    python_version,
                    f"{SQLITE_SUITE_PREFIX}regexp",
                    suite_environment,
                    "sqlite",
                    {"HARE_TEST_DB": "sqlite+aiosqlite://:memory:?install_regexp_functions=True"},
                    SQLITE_REGEXP_TEST_PATHS,
                    sqlite_version=newest_sqlite_version,
                )
            )
        for driver, url_template in MATRIX_POSTGRES_URLS.items():
            database = MATRIX_DATABASES_BY_DRIVER[driver]
            if database not in databases:
                continue
            for postgres_version in self.postgres_versions[-1:] if newest_only else self.postgres_versions:
                url = url_template.format(port=MatrixPorts.get_postgres_port(postgres_version))
                runs.append(
                    RunSpec(
                        python_version,
                        f"{driver}-{postgres_version}",
                        suite_environment,
                        database,
                        {"HARE_TEST_DB": url},
                        resources=frozenset({f"postgresql:{postgres_version}"}),
                    )
                )
        if not newest_only:
            runs.extend(self.get_pgbouncer_runs(python_version, suite_environment))
        runs.extend(self.get_clickhouse_runs(python_version, suite_environment))
        return runs

    def get_pgbouncer_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[RunSpec]:
        """The PostgreSQL suite through PgBouncer, by each driver the environment runs on: each PgBouncer in
        front of the newest PostgreSQL, and the newest PgBouncer in front of the oldest one - the PostgreSQL
        versions have a suite of their own each (the pairs of the Makefile's matrix_pgbouncer_pairs)."""
        pairs = [(pgbouncer_version, self.postgres_versions[-1]) for pgbouncer_version in self.pgbouncer_versions]
        pairs.append((self.pgbouncer_versions[-1], self.postgres_versions[0]))
        runs: list[RunSpec] = []
        for pgbouncer_version, postgres_version in dict.fromkeys(pairs):
            query = MATRIX_PGBOUNCER_URL_QUERY.format(direct_port=MatrixPorts.get_postgres_port(postgres_version))
            port = MatrixPorts.get_pgbouncer_port(pgbouncer_version, postgres_version)
            for driver, url_template in MATRIX_POSTGRES_URLS.items():
                database = MATRIX_DATABASES_BY_DRIVER[driver]
                if database not in suite_environment.databases:
                    continue
                url = url_template.format(port=port) + query
                suite = f"pgbouncer-{driver}-{pgbouncer_version}-{postgres_version}"
                runs.append(
                    RunSpec(
                        python_version,
                        suite,
                        suite_environment,
                        database,
                        {"HARE_TEST_DB": url},
                        resources=frozenset(
                            {f"postgresql:{postgres_version}", f"pgbouncer:{pgbouncer_version}:{postgres_version}"}
                        ),
                    )
                )
        return runs

    def get_clickhouse_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[RunSpec]:
        """The ClickHouse suite on each server and the cluster suite on each test cluster, by each driver the
        environment runs on."""
        runs: list[RunSpec] = []
        clickhouse_versions = (
            self.clickhouse_versions[-1:] if suite_environment.on_newest_servers_only else self.clickhouse_versions
        )
        for clickhouse_version in clickhouse_versions:
            for driver, url_template in MATRIX_CLICKHOUSE_URLS.items():
                database = MATRIX_DATABASES_BY_DRIVER[driver]
                if database not in suite_environment.databases:
                    continue
                keeper_hosts = f"127.0.0.1:{MatrixPorts.get_clickhouse_keeper_port(clickhouse_version)}"
                url = url_template.format(
                    port=MatrixPorts.get_clickhouse_port(clickhouse_version, driver, False), database="test_{}"
                ) + MATRIX_CLICKHOUSE_URL_QUERY.format(keeper_hosts=keeper_hosts)
                runs.append(
                    RunSpec(
                        python_version,
                        f"{driver}-{clickhouse_version}",
                        suite_environment,
                        database,
                        {"HARE_TEST_DB": url, "HARE_TEST_CLICKHOUSE_KEEPER_HOSTS": keeper_hosts},
                        MATRIX_CLICKHOUSE_TEST_PATHS,
                        resources=frozenset({f"clickhouse:{clickhouse_version}"}),
                    )
                )
                cluster_port = MatrixPorts.get_clickhouse_port(clickhouse_version, driver, True)
                cluster_url = url_template.format(port=cluster_port, database="test_cluster_{}")
                runs.append(
                    RunSpec(
                        python_version,
                        f"{driver}-cluster-{clickhouse_version}",
                        suite_environment,
                        database,
                        {"HARE_TEST_CLICKHOUSE_CLUSTER_DB": cluster_url + MATRIX_CLICKHOUSE_CLUSTER_URL_QUERY},
                        MATRIX_CLICKHOUSE_CLUSTER_TEST_PATHS,
                        resources=frozenset({f"clickhouse-cluster:{clickhouse_version}"}),
                    )
                )
        return runs

    def get_broker_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[RunSpec]:
        """The tests of a broker's environment on each version of that broker."""
        runs: list[RunSpec] = []
        for broker in suite_environment.extras:
            _, variable, value_template, port_prefix = MATRIX_BROKERS[broker]
            for broker_version in self.broker_versions[broker]:
                value = value_template.format(port=MatrixPorts.get_broker_port(port_prefix, broker_version))
                environment = {"HARE_TEST_DB": "sqlite+aiosqlite://:memory:", variable: value}
                runs.append(
                    RunSpec(
                        python_version,
                        f"{broker}-{broker_version}",
                        suite_environment,
                        "sqlite",
                        environment,
                        resources=frozenset({f"{broker}:{broker_version}"}),
                    )
                )
        return runs

    @staticmethod
    def get_log_path(run: RunSpec) -> Path:
        """The file the pytest output of a run is written to."""
        return MATRIX_LOG_DIRECTORY / f"{run.python_version}-{run.suite_environment.name}-{run.suite}.log"

    def execute(self, run: RunSpec) -> RunResult:
        """Execute a profile in a clean test environment and retain its actual pytest status."""
        MATRIX_LOG_DIRECTORY.mkdir(exist_ok=True)
        log_path = self.get_log_path(run)
        report_path = log_path.with_suffix(".json")
        report_path.unlink(missing_ok=True)
        command = [
            str(MatrixVirtualenv.get(run).get_python_executable()),
            "-m",
            "pytest",
            "-p",
            "tests.matrix.run_report",
            *([] if run.suite_environment.native else ["-p", WITHOUT_NATIVE_PLUGIN]),
            "-n",
            self.workers if run.suite_environment.whole_suite else "0",
            *MATRIX_PYTEST_OPTIONS,
            f"{SUITE_ENVIRONMENT_OPTION}={run.suite_environment.name}",
            "-m",
            run.marker_expression,
            *run.test_paths,
        ]
        environment = {name: value for name, value in os.environ.items() if not name.startswith("HARE_TEST_")}
        environment.update(run.environment)
        if run.sqlite_version is not None:
            environment.update(SqliteVersionEnvironment(run.sqlite_version, run.python_version).get_variables())
        environment[RUN_REPORT_ENVIRONMENT_VARIABLE] = str(report_path)
        started = time.perf_counter()
        with log_path.open("w", encoding="utf-8") as log_file:
            exit_code = self.processes.run(command, cwd=REPOSITORY_DIRECTORY, environment=environment, output=log_file)
        duration = time.perf_counter() - started
        return RunResult.read(run, exit_code, duration, log_path)

    @staticmethod
    def format_table(results: list[RunResult]) -> str:
        """Render observed outcomes without modifying their run specifications."""
        lines = [
            "| Python | Environment | Suite | Result | Passed | Failed | Errors | Skipped | Time, s |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for result in results:
            run = result.spec
            status = "passed" if result.passed else f"FAILED ({result.report_error or result.exit_code})"
            counts = result.counts
            lines.append(
                f"| {run.python_version} | {run.suite_environment.name} | {run.suite} | {status} | "
                f"{counts.get('passed', 0)} | {counts.get('failed', 0)} | {counts.get('errors', 0)} | "
                f"{counts.get('skipped', 0)} | {result.duration_seconds:.0f} |"
            )
        return "\n".join(lines)

    def create_virtualenvs(self, interpreter: str | None) -> None:
        """Creates the virtualenv of each run chosen, once each.

        Args:
            interpreter: The Python creating them - one of the only Python version chosen; ``py -X.Y`` or
                ``pythonX.Y`` when None.

        Raises:
            SystemExit: An interpreter is given for more than one Python version.
        """
        if interpreter is not None and len(self.python_versions) != 1:
            raise SystemExit("--interpreter is the Python of one version - name it with --python")
        for virtualenv in dict.fromkeys(MatrixVirtualenv.get(run) for run in self.get_runs()):
            print(f"{virtualenv.name}: {', '.join(virtualenv.extras) or 'no extras'}", flush=True)
            virtualenv.create(interpreter)

    @staticmethod
    def report_progress(completed: int, total: int, result: RunResult) -> None:
        run = result.spec
        print(
            f"[{completed}/{total}] Python {run.python_version} {run.suite_environment.name} {run.suite}: "
            f"exit {result.exit_code}, {result.counts}, {result.duration_seconds:.0f} s"
            f"{'; ' + result.report_error if result.report_error else ''}",
            flush=True,
        )

    def run(self) -> int:
        """Execute every cell while serializing conflicting server and cleanup resources."""
        runs = self.get_runs()
        self.processes = ProcessGroups()
        print(
            f"{len(runs)} runs: up to {self.jobs} full runs and {FEW_TEST_RUNS_AT_ONCE} small runs; "
            "shared resources serialized",
            flush=True,
        )
        results = RunScheduler(self.jobs, FEW_TEST_RUNS_AT_ONCE).run(
            runs,
            self.execute,
            self.processes.stop,
            self.report_progress,
        )
        print(self.format_table(results))
        failed = [result for result in results if not result.passed]
        if self.print_failed_logs:
            if isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout.reconfigure(errors="replace")
            for result in failed:
                run = result.spec
                print(f"Python {run.python_version} {run.suite_environment.name} {run.suite}: {result.log_path}")
                print(result.log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
        return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runs the test matrix.")
    parser.add_argument("--python", action="append", default=[], help="A Python version, e.g. 3.12")
    parser.add_argument(
        "--suite",
        action="append",
        default=[],
        help="A suite or a pattern of suites, e.g. sqlite-3.35.5, sqlite-* or asyncpg-14",
    )
    parser.add_argument(
        "--environment",
        action="append",
        default=[],
        help="An environment of SUITE_ENVIRONMENTS or a pattern of them, e.g. core or clickhouse-*",
    )
    parser.add_argument("--workers", default="8", help="pytest-xdist workers of each run, a number or auto")
    parser.add_argument("--jobs", type=int, default=1, help="How many runs go at once, e.g. one per server")
    parser.add_argument("--print-failed-logs", action="store_true", help="Print the pytest output of every failed run")
    parser.add_argument(
        "--newest",
        action="store_true",
        help="Only the newest Python (unless --python names others) and the newest version of each server",
    )
    parser.add_argument(
        "--virtualenvs", action="store_true", help="Create the virtualenvs of the runs chosen instead of running them"
    )
    parser.add_argument("--interpreter", help="The Python creating the virtualenvs of the one --python version")
    arguments = parser.parse_args()
    runner = MatrixRunner(
        arguments.python,
        arguments.suite,
        arguments.environment,
        arguments.workers,
        arguments.print_failed_logs,
        arguments.newest,
        arguments.jobs,
    )
    if arguments.virtualenvs:
        runner.create_virtualenvs(arguments.interpreter)
        raise SystemExit(0)
    raise SystemExit(runner.run())
