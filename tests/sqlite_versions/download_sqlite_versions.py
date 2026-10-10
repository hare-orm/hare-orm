"""Downloads the SQLite versions the test matrix runs against and prepares them for this Python.

Each version lands in ``tests/sqlite_versions/<version>/``:

- Windows: the official ``sqlite3.dll`` next to a copy of this Python's own ``_sqlite3.pyd``, in
  ``cp<major><minor>/`` - put that directory first on ``PYTHONPATH`` and ``import sqlite3`` loads it;
- Linux and macOS: ``libsqlite3`` built from the amalgamation - put the directory on
  ``LD_LIBRARY_PATH`` (``DYLD_LIBRARY_PATH`` on macOS). When this Python's own ``_sqlite3`` has
  SQLite linked in statically (the python.org macOS builds), the library path changes nothing: the
  ``_sqlite3`` module is then compiled from this Python's CPython sources against the built library
  into ``cp<major><minor>/``, which goes first on ``PYTHONPATH`` as on Windows.

``tests/sqlite_versions/environment.py`` prints the environment variables of one version.

Usage:
    python -m tests.sqlite_versions.download_sqlite_versions [VERSION ...] - "newest" names the newest one
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import sysconfig
import urllib.request
import zipfile
from pathlib import Path

from tests.sqlite_versions.constants import (
    NEWEST_SQLITE_VERSION_NAME,
    SQLITE_COMPILE_OPTIONS,
    SQLITE_DOWNLOAD_ROOT,
    SQLITE_RELEASES,
    SQLITE_VERSIONS_DIRECTORY,
)
from tests.sqlite_versions.environment import SqliteVersionEnvironment
from tests.sqlite_versions.sqlite_module_builder import SqliteModuleBuilder


class SqliteVersionDownloader:
    """Downloads and prepares the SQLite versions of the test matrix for the running Python."""

    def __init__(self, versions: list[str]) -> None:
        """
        Args:
            versions: The SQLite versions - ``NEWEST_SQLITE_VERSION_NAME`` for the newest one -
                every one of the matrix when empty.
        """
        newest_version = list(SQLITE_RELEASES)[-1]
        versions = [newest_version if version == NEWEST_SQLITE_VERSION_NAME else version for version in versions]
        unknown = sorted(set(versions) - set(SQLITE_RELEASES))
        if unknown:
            raise SystemExit(f"Unknown SQLite versions: {', '.join(unknown)} - known: {', '.join(SQLITE_RELEASES)}")
        self.versions = versions or list(SQLITE_RELEASES)

    def run(self) -> None:
        for version in self.versions:
            version_directory = SQLITE_VERSIONS_DIRECTORY / version
            version_directory.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                self.prepare_windows(version, version_directory)
            else:
                library_path = self.build_library(version, version_directory)
                if self.get_loaded_version(version) != version:
                    module_path = SqliteModuleBuilder(version_directory, library_path, self.get_compiler()).build()
                    print(f"SQLite {version}: {module_path}")
            loaded_version = self.get_loaded_version(version)
            if loaded_version != version:
                raise SystemExit(f"SQLite {version}: this Python loads SQLite {loaded_version} in its environment")

    @staticmethod
    def get_loaded_version(version: str) -> str:
        """The SQLite version a Python like this one loads in the environment of a prepared version.

        Args:
            version: The prepared SQLite version.

        Returns:
            ``sqlite3.sqlite_version`` of a child Python started in that environment.
        """
        environment = {**os.environ, **SqliteVersionEnvironment(version).get_variables()}
        completed = subprocess.run(
            [sys.executable, "-c", "import sqlite3; print(sqlite3.sqlite_version)"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def download(path: str) -> bytes:
        """The bytes of a file of sqlite.org."""
        with urllib.request.urlopen(f"{SQLITE_DOWNLOAD_ROOT}/{path}", timeout=120) as response:  # noqa: S310
            return response.read()

    @classmethod
    def prepare_windows(cls, version: str, version_directory: Path) -> None:
        """The official DLL of ``version`` next to this Python's ``_sqlite3.pyd``."""
        python_directory = version_directory / f"cp{sys.version_info.major}{sys.version_info.minor}"
        python_directory.mkdir(exist_ok=True)
        dll_path = version_directory / "sqlite3.dll"
        if not dll_path.exists():
            archive = zipfile.ZipFile(io.BytesIO(cls.download(SQLITE_RELEASES[version]["windows_dll"])))
            dll_path.write_bytes(archive.read("sqlite3.dll"))
        shutil.copyfile(dll_path, python_directory / "sqlite3.dll")
        own_extension = Path(sys.base_prefix) / "DLLs" / "_sqlite3.pyd"
        shutil.copyfile(own_extension, python_directory / "_sqlite3.pyd")
        print(f"SQLite {version}: {python_directory}")

    @staticmethod
    def get_compiler() -> str:
        """The C compiler this Python was built with, ``cc`` when that one isn't on this machine.

        Returns:
            The compiler's executable.
        """
        compiler = (sysconfig.get_config_var("CC") or "cc").split()[0]
        # Relocated builds (python-build-standalone, which uv installs) name the compiler of the
        # machine they were built on.
        return compiler if shutil.which(compiler) else "cc"

    @classmethod
    def build_library(cls, version: str, version_directory: Path) -> Path:
        """``libsqlite3`` of ``version`` compiled from its amalgamation, with its ``sqlite3.h`` next to it.

        Returns:
            The library.
        """
        library_name = "libsqlite3.0.dylib" if sys.platform == "darwin" else "libsqlite3.so.0"
        library_path = version_directory / library_name
        if library_path.exists():
            print(f"SQLite {version}: {library_path}")
            return library_path
        archive = zipfile.ZipFile(io.BytesIO(cls.download(SQLITE_RELEASES[version]["amalgamation"])))
        for file_name in ("sqlite3.c", "sqlite3.h"):
            member_name = next(name for name in archive.namelist() if name.endswith(f"/{file_name}"))
            (version_directory / file_name).write_bytes(archive.read(member_name))
        source_path = version_directory / "sqlite3.c"
        command = [
            cls.get_compiler(),
            "-shared",
            "-fPIC",
            "-O2",
            *SQLITE_COMPILE_OPTIONS,
            str(source_path),
            "-o",
            str(library_path),
            "-lpthread",
            "-lm",
        ]
        if sys.platform == "darwin":
            command.extend(["-install_name", f"@rpath/{library_name}"])
        else:
            command.extend(["-ldl", f"-Wl,-soname,{library_name}"])
        subprocess.run(command, check=True)
        source_path.unlink()
        print(f"SQLite {version}: {library_path}")
        return library_path


if __name__ == "__main__":
    SqliteVersionDownloader(sys.argv[1:]).run()
