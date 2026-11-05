"""Prints the environment variables that make a test run load one prepared SQLite version, as
``NAME=value`` lines for ``env``.

Usage:
    python -m tests.sqlite_versions.environment VERSION
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.artifacts.artifact_cache import ArtifactCache
from tests.artifacts.build_spec import BuildSpec
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
        """Select only complete artifacts, rejecting stale ABI or damaged cached files."""
        version_directory = SQLITE_VERSIONS_DIRECTORY / self.version
        selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        paths = {name: version_directory / value if value else None for name, value in selection.items()}
        for path in set(paths.values()) - {None}:
            spec = ArtifactCache.get_spec(path)
            if spec is None:
                raise SystemExit(f"SQLite {self.version}: incomplete artifact {path}; prepare this version again")
            if (
                "python" in spec.description
                and self.python_version == f"{sys.version_info.major}.{sys.version_info.minor}"
                and spec.description["python"] != BuildSpec.get_python_abi()
            ):
                raise SystemExit(f"SQLite {self.version}: the artifact ABI differs from this Python; prepare it again")
        return self.make_variables(paths["library"], paths["module"])

    @property
    def selection_path(self) -> Path:
        """The minor-version label selects a manifest; it is never an artifact identity."""
        return SQLITE_VERSIONS_DIRECTORY / self.version / f"cp{self.python_version.replace('.', '')}.json"

    def publish(self, library_directory: Path | None, python_directory: Path | None) -> None:
        """Publish paths after the target Python has loaded the requested SQLite version."""
        root = SQLITE_VERSIONS_DIRECTORY / self.version
        ArtifactCache.write_json(
            self.selection_path,
            {
                "library": library_directory.relative_to(root).as_posix() if library_directory else None,
                "module": python_directory.relative_to(root).as_posix() if python_directory else None,
            },
        )

    def make_variables(self, library_directory: Path | None, python_directory: Path | None) -> dict[str, str]:
        """Build variables for explicit published or staging paths, without selecting a cache entry."""
        variables = {SQLITE_VERSION_ENVIRONMENT_VARIABLE: self.version}
        if python_directory is not None:
            search_path = [str(python_directory), os.environ.get("PYTHONPATH")]
            variables["PYTHONPATH"] = os.pathsep.join(filter(None, search_path))
        if sys.platform != "win32" and library_directory is not None:
            name = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
            variables[name] = os.pathsep.join(filter(None, [str(library_directory), os.environ.get(name)]))
        return variables

    def get_loaded_version(self, library_directory: Path | None, python_directory: Path | None) -> str:
        """Probe in a fresh target interpreter, requiring its extension to come from the selected path."""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sqlite3, _sqlite3; print(json.dumps([sqlite3.sqlite_version, _sqlite3.__file__]))",
            ],
            env={**os.environ, **self.make_variables(library_directory, python_directory)},
            capture_output=True,
            text=True,
            check=True,
        )
        version, extension = json.loads(completed.stdout)
        if python_directory is not None and Path(extension).parent != python_directory.absolute():
            raise SystemExit(f"SQLite {self.version}: the probe loaded {extension} instead of the staged extension")
        return version

    def verify(self, library_directory: Path | None, python_directory: Path | None) -> None:
        """Refuse publication when loading succeeds with a different SQLite version."""
        loaded = self.get_loaded_version(library_directory, python_directory)
        if loaded != self.version:
            raise SystemExit(f"SQLite {self.version}: this Python loads SQLite {loaded} in its environment")


if __name__ == "__main__":
    for name, value in SqliteVersionEnvironment(sys.argv[1]).get_variables().items():
        print(f"{name}={value}")
