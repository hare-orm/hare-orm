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
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    MATRIX_LOG_DIRECTORY,
    MATRIX_PGBOUNCER_URL_QUERY,
    MATRIX_PGBOUNCER_VERSIONS,
    MATRIX_POSTGRES_URLS,
    MATRIX_POSTGRES_VERSIONS,
    MATRIX_PYTEST_OPTIONS,
    MATRIX_PYTHON_VERSIONS,
    PYTEST_COUNT_PATTERN,
    PYTEST_DURATION_PATTERN,
    PYTEST_NO_TESTS_EXIT_CODE,
    REPOSITORY_DIRECTORY,
    SQLITE_REGEXP_TEST_PATHS,
    SQLITE_SUITE_PREFIX,
    SUITE_ENVIRONMENT_OPTION,
    SUITE_ENVIRONMENTS,
    WITHOUT_NATIVE_PLUGIN,
)
from tests.matrix.extra_test_modules import ExtraTestModules
from tests.matrix.matrix_ports import MatrixPorts
from tests.matrix.matrix_run import MatrixRun
from tests.matrix.matrix_virtualenv import MatrixVirtualenv
from tests.matrix.suite_environment import SuiteEnvironment
from tests.sqlite_versions.constants import SQLITE_RELEASES
from tests.sqlite_versions.environment import SqliteVersionEnvironment


class MatrixRunner:
    """Runs the test matrix, one run after another, and reports it."""

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
            jobs: How many runs go at once - each a pytest of its own with ``workers`` workers; runs on
                one server share it safely, every test process naming its databases apart.

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

    def get_runs(self) -> list[MatrixRun]:
        """The runs of the matrix, filtered by the requested suites and environments.

        Raises:
            SystemExit: A requested suite isn't part of the matrix.
        """
        runs: list[MatrixRun] = []
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

    def get_environment_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[MatrixRun]:
        """The runs of an environment's tests on one Python: the tests reading no database, and the suite of
        each database the environment runs on - an environment of a broker's extra on that broker's servers.
        """
        # The tests reading no test database - once per Python and environment, not on every database; an
        # environment of extras collects only the modules that may hold its tests.
        test_paths = () if suite_environment.whole_suite else ExtraTestModules.get_test_paths(suite_environment)
        runs = (
            [
                MatrixRun(
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
                environment = SqliteVersionEnvironment(sqlite_version, python_version).get_variables()
                environment["HARE_TEST_DB"] = "sqlite+aiosqlite://:memory:"
                runs.append(
                    MatrixRun(
                        python_version,
                        f"{SQLITE_SUITE_PREFIX}{sqlite_version}",
                        suite_environment,
                        "sqlite",
                        environment,
                    )
                )
            newest_sqlite_version = list(SQLITE_RELEASES)[-1]
            regexp_environment = SqliteVersionEnvironment(newest_sqlite_version, python_version).get_variables()
            regexp_environment["HARE_TEST_DB"] = "sqlite+aiosqlite://:memory:?install_regexp_functions=True"
            runs.append(
                MatrixRun(
                    python_version,
                    f"{SQLITE_SUITE_PREFIX}regexp",
                    suite_environment,
                    "sqlite",
                    regexp_environment,
                    SQLITE_REGEXP_TEST_PATHS,
                )
            )
        for driver, url_template in MATRIX_POSTGRES_URLS.items():
            database = MATRIX_DATABASES_BY_DRIVER[driver]
            if database not in databases:
                continue
            for postgres_version in self.postgres_versions[-1:] if newest_only else self.postgres_versions:
                url = url_template.format(port=MatrixPorts.get_postgres_port(postgres_version))
                runs.append(
                    MatrixRun(
                        python_version,
                        f"{driver}-{postgres_version}",
                        suite_environment,
                        database,
                        {"HARE_TEST_DB": url},
                    )
                )
        if not newest_only:
            runs.extend(self.get_pgbouncer_runs(python_version, suite_environment))
        runs.extend(self.get_clickhouse_runs(python_version, suite_environment))
        return runs

    def get_pgbouncer_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[MatrixRun]:
        """The PostgreSQL suite through PgBouncer, by each driver the environment runs on: each PgBouncer in
        front of the newest PostgreSQL, and the newest PgBouncer in front of the oldest one - the PostgreSQL
        versions have a suite of their own each (the pairs of the Makefile's matrix_pgbouncer_pairs)."""
        pairs = [(pgbouncer_version, self.postgres_versions[-1]) for pgbouncer_version in self.pgbouncer_versions]
        pairs.append((self.pgbouncer_versions[-1], self.postgres_versions[0]))
        runs: list[MatrixRun] = []
        for pgbouncer_version, postgres_version in dict.fromkeys(pairs):
            query = MATRIX_PGBOUNCER_URL_QUERY.format(direct_port=MatrixPorts.get_postgres_port(postgres_version))
            port = MatrixPorts.get_pgbouncer_port(pgbouncer_version, postgres_version)
            for driver, url_template in MATRIX_POSTGRES_URLS.items():
                database = MATRIX_DATABASES_BY_DRIVER[driver]
                if database not in suite_environment.databases:
                    continue
                url = url_template.format(port=port) + query
                suite = f"pgbouncer-{driver}-{pgbouncer_version}-{postgres_version}"
                runs.append(MatrixRun(python_version, suite, suite_environment, database, {"HARE_TEST_DB": url}))
        return runs

    def get_clickhouse_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[MatrixRun]:
        """The ClickHouse suite on each server and the cluster suite on each test cluster, by each driver the
        environment runs on."""
        runs: list[MatrixRun] = []
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
                    MatrixRun(
                        python_version,
                        f"{driver}-{clickhouse_version}",
                        suite_environment,
                        database,
                        {"HARE_TEST_DB": url, "HARE_TEST_CLICKHOUSE_KEEPER_HOSTS": keeper_hosts},
                        MATRIX_CLICKHOUSE_TEST_PATHS,
                    )
                )
                cluster_port = MatrixPorts.get_clickhouse_port(clickhouse_version, driver, True)
                cluster_url = url_template.format(port=cluster_port, database="test_cluster_{}")
                runs.append(
                    MatrixRun(
                        python_version,
                        f"{driver}-cluster-{clickhouse_version}",
                        suite_environment,
                        database,
                        {"HARE_TEST_CLICKHOUSE_CLUSTER_DB": cluster_url + MATRIX_CLICKHOUSE_CLUSTER_URL_QUERY},
                        MATRIX_CLICKHOUSE_CLUSTER_TEST_PATHS,
                    )
                )
        return runs

    def get_broker_runs(self, python_version: str, suite_environment: SuiteEnvironment) -> list[MatrixRun]:
        """The tests of a broker's environment on each version of that broker."""
        runs: list[MatrixRun] = []
        for broker in suite_environment.extras:
            _, variable, value_template, port_prefix = MATRIX_BROKERS[broker]
            for broker_version in self.broker_versions[broker]:
                value = value_template.format(port=MatrixPorts.get_broker_port(port_prefix, broker_version))
                environment = {"HARE_TEST_DB": "sqlite+aiosqlite://:memory:", variable: value}
                runs.append(
                    MatrixRun(python_version, f"{broker}-{broker_version}", suite_environment, "sqlite", environment)
                )
        return runs

    @staticmethod
    def get_log_path(run: MatrixRun) -> Path:
        """The file the pytest output of a run is written to."""
        return MATRIX_LOG_DIRECTORY / f"{run.python_version}-{run.suite_environment.name}-{run.suite}.log"

    def execute(self, run: MatrixRun) -> None:
        """Runs one cell and records its outcome on it - one choosing no test of its environment passes."""
        MATRIX_LOG_DIRECTORY.mkdir(exist_ok=True)
        log_path = self.get_log_path(run)
        command = [
            str(MatrixVirtualenv.get(run).get_python_executable()),
            "-m",
            "pytest",
            *([] if run.suite_environment.native else ["-p", WITHOUT_NATIVE_PLUGIN]),
            "-n",
            # A run of an environment of extras holds a few tests - one process collects the suite once.
            self.workers if run.suite_environment.whole_suite else "0",
            *MATRIX_PYTEST_OPTIONS,
            f"{SUITE_ENVIRONMENT_OPTION}={run.suite_environment.name}",
            "-m",
            run.marker_expression,
            *run.test_paths,
        ]
        environment = {**os.environ, **run.environment}
        started = time.perf_counter()
        with log_path.open("w", encoding="utf-8") as log_file:
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_DIRECTORY,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
        run.exit_code = 0 if completed.returncode == PYTEST_NO_TESTS_EXIT_CODE else completed.returncode
        summary = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
        summary_line = summary[0] if summary else ""
        run.counts = {
            ("errors" if outcome.startswith("error") else outcome): int(count)
            for count, outcome in re.findall(PYTEST_COUNT_PATTERN, summary_line)
        }
        duration = re.search(PYTEST_DURATION_PATTERN, summary_line)
        run.duration_seconds = float(duration.group(1)) if duration else time.perf_counter() - started

    @staticmethod
    def format_table(runs: list[MatrixRun]) -> str:
        """The outcomes as a Markdown table."""
        lines = [
            "| Python | Environment | Suite | Result | Passed | Failed | Errors | Skipped | Time, s |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for run in runs:
            result = "passed" if run.exit_code == 0 else f"FAILED ({run.exit_code})"
            counts = run.counts
            lines.append(
                f"| {run.python_version} | {run.suite_environment.name} | {run.suite} | {result} | "
                f"{counts.get('passed', 0)} | {counts.get('failed', 0)} | {counts.get('errors', 0)} | "
                f"{counts.get('skipped', 0)} | {run.duration_seconds or 0:.0f} |"
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

    def run(self) -> int:
        """Runs every cell and prints the table.

        Returns:
            0 when every run passed, 1 otherwise.
        """
        runs = self.get_runs()
        # A few tests of an environment of extras reaching no server - several such runs at once.
        few_test_runs = [run for run in runs if not run.suite_environment.whole_suite and not run.reaches_a_server()]
        other_runs = [run for run in runs if run not in few_test_runs]
        print(
            f"{len(runs)} runs: {len(other_runs)} {self.jobs} at once, {len(few_test_runs)} of a few tests reaching "
            f"no server {FEW_TEST_RUNS_AT_ONCE} at once",
            flush=True,
        )
        with (
            ThreadPoolExecutor(max_workers=self.jobs) as other_executor,
            ThreadPoolExecutor(max_workers=FEW_TEST_RUNS_AT_ONCE) as few_test_executor,
        ):
            futures = {other_executor.submit(self.execute, run): run for run in other_runs} | {
                few_test_executor.submit(self.execute, run): run for run in few_test_runs
            }
            for index, future in enumerate(as_completed(futures), start=1):
                future.result()
                run = futures[future]
                print(
                    f"[{index}/{len(runs)}] Python {run.python_version} {run.suite_environment.name} {run.suite}: "
                    f"exit {run.exit_code}, {run.counts}, {run.duration_seconds:.0f} s",
                    flush=True,
                )
        print(self.format_table(runs))
        failed_runs = [run for run in runs if run.exit_code != 0]
        if self.print_failed_logs:
            # A log holds text the console's encoding may lack (cp1251 on Windows) - printed replaced, not raised.
            if isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout.reconfigure(errors="replace")
            for run in failed_runs:
                print(
                    f"\n===== Python {run.python_version} {run.suite_environment.name} {run.suite}: "
                    f"{self.get_log_path(run)} ====="
                )
                print(self.get_log_path(run).read_text(encoding="utf-8", errors="replace"), flush=True)
        return 1 if failed_runs else 0


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
