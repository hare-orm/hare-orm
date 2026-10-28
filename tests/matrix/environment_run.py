"""Runs the tests of one environment of ``SUITE_ENVIRONMENTS`` on the database of HARE_TEST_DB, in the
virtualenv of the environment and of that database's driver (``make matrix_venvs`` creates it).

Usage:
    python -m tests.matrix.environment_run PYTHON_VERSION ENVIRONMENT [PYTEST_ARGUMENT ...]
"""

from __future__ import annotations

import os
import subprocess
import sys

from tests.matrix.constants import (
    MATRIX_DATABASE_DRIVER_EXTRAS,
    REPOSITORY_DIRECTORY,
    SUITE_ENVIRONMENT_OPTION,
    SUITE_ENVIRONMENTS,
    WITHOUT_NATIVE_PLUGIN,
)
from tests.matrix.matrix_run import MatrixRun
from tests.matrix.matrix_virtualenv import MatrixVirtualenv


class EnvironmentRun:
    """A run of one environment's tests on the database under test."""

    @staticmethod
    def get_database(db_url: str) -> str:
        """The database of a DB_URL, as ``MATRIX_DATABASE_DRIVER_EXTRAS`` names it.

        Args:
            db_url: The URL.

        Returns:
            The database.

        Raises:
            SystemExit: No database of the matrix has the URL's scheme.
        """
        scheme = db_url.split("://", 1)[0]
        database = "sqlite" if scheme.startswith("sqlite") else scheme
        if database not in MATRIX_DATABASE_DRIVER_EXTRAS:
            raise SystemExit(f"HARE_TEST_DB's scheme {scheme} is no database of the matrix")
        return database

    @staticmethod
    def run(python_version: str, environment_name: str, pytest_arguments: list[str]) -> int:
        """Runs the tests.

        Args:
            python_version: The Python of the virtualenv.
            environment_name: The environment.
            pytest_arguments: pytest's other arguments.

        Returns:
            pytest's exit code.

        Raises:
            SystemExit: No environment has the name, or its virtualenv doesn't exist.
        """
        environments = {environment.name: environment for environment in SUITE_ENVIRONMENTS}
        if environment_name not in environments:
            raise SystemExit(f"No environment {environment_name} - known: {', '.join(environments)}")
        environment = environments[environment_name]
        database = EnvironmentRun.get_database(os.environ.get("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"))
        virtualenv = MatrixVirtualenv.get(MatrixRun(python_version, environment_name, environment, database, {}))
        if not virtualenv.get_python_executable().exists():
            raise SystemExit(
                f"{virtualenv.name} doesn't exist - make matrix_venvs MATRIX_OPTIONS=\"--python {python_version} "
                f'--environment {environment_name}"'
            )
        command = [
            str(virtualenv.get_python_executable()),
            "-m",
            "pytest",
            *([] if environment.native else ["-p", WITHOUT_NATIVE_PLUGIN]),
            f"{SUITE_ENVIRONMENT_OPTION}={environment_name}",
            *pytest_arguments,
        ]
        return subprocess.run(command, cwd=REPOSITORY_DIRECTORY, check=False).returncode


if __name__ == "__main__":
    sys.exit(EnvironmentRun.run(sys.argv[1], sys.argv[2], sys.argv[3:]))
