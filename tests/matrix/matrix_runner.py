"""Runs the whole test suite on every Python, SQLite and PostgreSQL version of the matrix and prints
a table of the outcomes. The tests reading no test database are a suite of their own, run once per
Python.

Needs the virtualenvs ``.venv-3.12``/``.venv-3.13``/``.venv-3.14``, the SQLite versions
(``tests/sqlite_versions/download_sqlite_versions.py`` run by each Python) and the PostgreSQL
servers (``make test_db_matrix_up``).

Usage:
    python -m tests.matrix.matrix_runner [--python 3.12 ...] [--suite sqlite-3.35.5 ...] [--workers 8]
        [--print-failed-logs] [--newest]
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from tests.matrix.constants import (
    DATABASE_INDEPENDENT_MARKER,
    DATABASE_INDEPENDENT_SUITE,
    MATRIX_LOG_DIRECTORY,
    MATRIX_POSTGRES_URLS,
    MATRIX_POSTGRES_VERSIONS,
    MATRIX_PYTEST_OPTIONS,
    MATRIX_PYTHON_VERSIONS,
    PYTEST_COUNT_PATTERN,
    PYTEST_DURATION_PATTERN,
    REPOSITORY_DIRECTORY,
    SQLITE_REGEXP_TEST_PATHS,
)
from tests.matrix.matrix_run import MatrixRun
from tests.sqlite_versions.constants import SQLITE_RELEASES
from tests.sqlite_versions.environment import SqliteVersionEnvironment


class MatrixRunner:
    """Runs the test matrix, one run after another, and reports it."""

    def __init__(
        self, python_versions: list[str], suites: list[str], workers: str, print_failed_logs: bool, newest: bool
    ) -> None:
        """
        Args:
            python_versions: The Python versions to run on, every one of the matrix when empty -
                the newest one with ``newest``.
            suites: The suites to run (``sqlite-3.35.5``, ``columnar``, ``asyncpg-14``, ...) or
                shell-style patterns of them (``sqlite-*``), every one when empty.
            workers: pytest-xdist workers of each run - a number or ``auto``.
            print_failed_logs: Whether the pytest output of every failed run is printed after the
                table.
            newest: Whether only the newest SQLite and PostgreSQL versions of the matrix run.

        Raises:
            SystemExit: A Python version or a suite isn't part of the matrix.
        """
        unknown_python_versions = sorted(set(python_versions) - set(MATRIX_PYTHON_VERSIONS))
        if unknown_python_versions:
            raise SystemExit(
                f"Unknown Python versions: {', '.join(unknown_python_versions)} - "
                f"known: {', '.join(MATRIX_PYTHON_VERSIONS)}"
            )
        self.python_versions = python_versions or list(
            MATRIX_PYTHON_VERSIONS[-1:] if newest else MATRIX_PYTHON_VERSIONS
        )
        self.sqlite_versions = list(SQLITE_RELEASES)[-1:] if newest else list(SQLITE_RELEASES)
        self.postgres_versions = list(MATRIX_POSTGRES_VERSIONS[-1:] if newest else MATRIX_POSTGRES_VERSIONS)
        self.suites = suites
        self.workers = workers
        self.print_failed_logs = print_failed_logs

    def get_runs(self) -> list[MatrixRun]:
        """The runs of the matrix, filtered by the requested suites.

        Raises:
            SystemExit: A requested suite isn't part of the matrix.
        """
        runs: list[MatrixRun] = []
        newest_sqlite_version = list(SQLITE_RELEASES)[-1]
        for python_version in self.python_versions:
            # The tests reading no test database - once per Python, not once more on every database.
            runs.append(
                MatrixRun(
                    python_version,
                    DATABASE_INDEPENDENT_SUITE,
                    {"HARE_TEST_DB": "sqlite+aiosqlite://:memory:"},
                    marker_expression=DATABASE_INDEPENDENT_MARKER,
                )
            )
            for sqlite_version in self.sqlite_versions:
                environment = SqliteVersionEnvironment(sqlite_version, python_version).get_variables()
                environment["HARE_TEST_DB"] = "sqlite+aiosqlite://:memory:"
                runs.append(MatrixRun(python_version, f"sqlite-{sqlite_version}", environment))
            regexp_environment = SqliteVersionEnvironment(newest_sqlite_version, python_version).get_variables()
            regexp_environment["HARE_TEST_DB"] = "sqlite+aiosqlite://:memory:?install_regexp_functions=True"
            runs.append(MatrixRun(python_version, "sqlite-regexp", regexp_environment, SQLITE_REGEXP_TEST_PATHS))
            runs.append(MatrixRun(python_version, "columnar", {"HARE_TEST_DB": "columnar://:memory:"}))
            for driver, url_template in MATRIX_POSTGRES_URLS.items():
                for postgres_version in self.postgres_versions:
                    url = url_template.format(port=f"54{postgres_version}")
                    runs.append(MatrixRun(python_version, f"{driver}-{postgres_version}", {"HARE_TEST_DB": url}))
        if self.suites:
            known_suites = list(dict.fromkeys(run.suite for run in runs))
            unknown_suites = [pattern for pattern in self.suites if not fnmatch.filter(known_suites, pattern)]
            if unknown_suites:
                raise SystemExit(f"No suite matches {', '.join(unknown_suites)} - known: {', '.join(known_suites)}")
            runs = [run for run in runs if any(fnmatch.fnmatchcase(run.suite, pattern) for pattern in self.suites)]
        return runs

    @staticmethod
    def get_log_path(run: MatrixRun) -> Path:
        """The file the pytest output of a run is written to."""
        return MATRIX_LOG_DIRECTORY / f"{run.python_version}-{run.suite}.log"

    @staticmethod
    def get_python_executable(python_version: str) -> Path:
        """The Python of the version's virtualenv."""
        virtualenv = REPOSITORY_DIRECTORY / f".venv-{python_version}"
        return virtualenv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    def execute(self, run: MatrixRun) -> None:
        """Runs one cell and records its outcome on it."""
        MATRIX_LOG_DIRECTORY.mkdir(exist_ok=True)
        log_path = self.get_log_path(run)
        command = [
            str(self.get_python_executable(run.python_version)),
            "-m",
            "pytest",
            "-n",
            self.workers,
            *MATRIX_PYTEST_OPTIONS,
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
        run.exit_code = completed.returncode
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
            "| Python | Suite | Result | Passed | Failed | Errors | Skipped | Time, s |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for run in runs:
            result = "passed" if run.exit_code == 0 else f"FAILED ({run.exit_code})"
            counts = run.counts
            lines.append(
                f"| {run.python_version} | {run.suite} | {result} | {counts.get('passed', 0)} | "
                f"{counts.get('failed', 0)} | {counts.get('errors', 0)} | {counts.get('skipped', 0)} | "
                f"{run.duration_seconds or 0:.0f} |"
            )
        return "\n".join(lines)

    def run(self) -> int:
        """Runs every cell and prints the table.

        Returns:
            0 when every run passed, 1 otherwise.
        """
        runs = self.get_runs()
        for index, run in enumerate(runs, start=1):
            print(f"[{index}/{len(runs)}] Python {run.python_version} {run.suite} ...", flush=True)
            self.execute(run)
            print(f"    exit {run.exit_code}, {run.counts}, {run.duration_seconds:.0f} s", flush=True)
        print(self.format_table(runs))
        failed_runs = [run for run in runs if run.exit_code != 0]
        if self.print_failed_logs:
            for run in failed_runs:
                print(f"\n===== Python {run.python_version} {run.suite}: {self.get_log_path(run)} =====")
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
    parser.add_argument("--workers", default="8", help="pytest-xdist workers of each run, a number or auto")
    parser.add_argument("--print-failed-logs", action="store_true", help="Print the pytest output of every failed run")
    parser.add_argument(
        "--newest",
        action="store_true",
        help="Only the newest Python (unless --python names others), SQLite and PostgreSQL",
    )
    arguments = parser.parse_args()
    sys.exit(
        MatrixRunner(
            arguments.python, arguments.suite, arguments.workers, arguments.print_failed_logs, arguments.newest
        ).run()
    )
