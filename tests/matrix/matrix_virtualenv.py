from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

from tests.matrix.constants import MATRIX_DATABASE_DRIVER_EXTRAS
from tests.matrix.matrix_catalog import MatrixCatalog
from tests.matrix.paths import REPOSITORY_DIRECTORY
from tests.matrix.suite_environment import SuiteEnvironment


@dataclasses.dataclass(frozen=True)
class MatrixVirtualenv:
    """The virtualenv of a run of the matrix: hare with the extras and groups of the run's environment and
    the extra of its database's driver - nothing else optional.

    Attributes:
        python_version: Its Python.
        name: Its directory's name in the repository - ``.venv-3.14-core``, ``.venv-3.14-core-asyncpg``.
        extras: The extras installed.
        groups: The poetry groups installed besides ``main`` and ``test``.
    """

    python_version: str
    name: str
    extras: tuple[str, ...]
    groups: tuple[str, ...]

    @classmethod
    def get(cls, python_version: str, environment: SuiteEnvironment, database: str) -> MatrixVirtualenv:
        """The virtualenv for this Python, suite environment and database driver."""
        driver_extras = tuple(
            extra for extra in MATRIX_DATABASE_DRIVER_EXTRAS[database] if extra not in environment.extras
        )
        if MatrixCatalog.get_unsupported_extras(python_version).intersection(driver_extras):
            raise SystemExit(f"{database} is not available on Python {python_version}")
        name = "-".join([f".venv-{python_version}", environment.name, *driver_extras])
        return cls(python_version, name, (*environment.extras, *driver_extras), environment.groups)

    def get_path(self) -> Path:
        """The virtualenv's directory."""
        return REPOSITORY_DIRECTORY / self.name

    def get_python_executable(self) -> Path:
        """The virtualenv's Python."""
        return self.get_path() / "bin/python"

    def create(self, interpreter: str | None) -> None:
        """Creates the virtualenv and installs hare into it - ``poetry install`` of the main and test
        groups, the groups and the extras.

        Args:
            interpreter: The Python creating it; ``pythonX.Y`` when None.

        Raises:
            subprocess.CalledProcessError: Creating it or installing into it failed.
        """
        creator = [interpreter if interpreter is not None else f"python{self.python_version}"]
        # --clear: a virtualenv made again holds only what its environment installs, never a package left
        # from an earlier install.
        subprocess.run([*creator, "-m", "venv", "--clear", str(self.get_path())], cwd=REPOSITORY_DIRECTORY, check=True)
        command = ["poetry", "install", "--only", ",".join(("main", "test", *self.groups))]
        for extra in self.extras:
            command += ["--extras", extra]
        subprocess.run(
            command, cwd=REPOSITORY_DIRECTORY, env={**os.environ, "VIRTUAL_ENV": str(self.get_path())}, check=True
        )
