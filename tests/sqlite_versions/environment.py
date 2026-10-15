"""Prints the environment variables that make a test run load one prepared SQLite version, as
``NAME=value`` lines for ``env``.

Usage:
    python -m tests.sqlite_versions.environment VERSION
"""

from __future__ import annotations

import os
import sys

from tests.sqlite_versions.constants import (
    SQLITE_RELEASES,
    SQLITE_VERSION_ENVIRONMENT_VARIABLE,
    SQLITE_VERSIONS_DIRECTORY,
)


class SqliteVersionEnvironment:
    """The environment of a test run on one prepared SQLite version."""

    def __init__(self, version: str, python_version: str | None = None) -> None:
        """
        Args:
            version: The SQLite version.
            python_version: The ``major.minor`` of the Python the run uses, this one's by default.
        """
        if version not in SQLITE_RELEASES:
            raise SystemExit(f"Unknown SQLite version {version} - known: {', '.join(SQLITE_RELEASES)}")
        self.version = version
        self.python_version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"

    def get_variables(self) -> dict[str, str]:
        """The variables by name."""
        version_directory = SQLITE_VERSIONS_DIRECTORY / self.version
        variables = {SQLITE_VERSION_ENVIRONMENT_VARIABLE: self.version}
        # The version's own _sqlite3 module for this Python: on Windows always, elsewhere when the
        # Python's module has SQLite linked in statically.
        python_directory = version_directory / f"cp{self.python_version.replace('.', '')}"
        if sys.platform == "win32" or python_directory.is_dir():
            search_path = [str(python_directory), os.environ.get("PYTHONPATH")]
            variables["PYTHONPATH"] = os.pathsep.join(filter(None, search_path))
        if sys.platform != "win32":
            name = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
            variables[name] = os.pathsep.join(filter(None, [str(version_directory), os.environ.get(name)]))
        return variables


if __name__ == "__main__":
    for name, value in SqliteVersionEnvironment(sys.argv[1]).get_variables().items():
        print(f"{name}={value}")
