"""Builds CPython's ``_sqlite3`` module against one prepared ``libsqlite3``.

A Python whose own ``_sqlite3`` keeps its SQLite whatever the library search path says - SQLite
linked in statically (the python.org macOS builds, which ``actions/setup-python`` installs on macOS),
or found through the module's own ``RPATH`` (the manylinux builds) - gets the module compiled from
the CPython sources of the running version and linked to the prepared library;
``tests/sqlite_versions/<version>/cp<major><minor>/`` put first on ``PYTHONPATH`` loads it instead
of the Python's own one.
"""

from __future__ import annotations

import io
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import urllib.request
from pathlib import Path

from tests.sqlite_versions.constants import (
    CPYTHON_SOURCE_URL,
    CPYTHON_SQLITE_MODULE_DIRECTORY,
    SQLITE_VERSIONS_DIRECTORY,
)


class SqliteModuleBuilder:
    """Builds ``_sqlite3`` for the running Python against one prepared ``libsqlite3``."""

    def __init__(self, version_directory: Path, library_path: Path, compiler: str) -> None:
        """
        Args:
            version_directory: The directory of the prepared SQLite version, holding its
                ``sqlite3.h``.
            library_path: The prepared ``libsqlite3``.
            compiler: The C compiler's executable.
        """
        self.version_directory = version_directory
        self.library_path = library_path
        self.compiler = compiler

    @staticmethod
    def get_module_directory() -> Path:
        """The directory of ``Modules/_sqlite`` of the running Python's CPython sources, downloaded once.

        Returns:
            The directory.
        """
        python_version = platform.python_version()
        module_directory = SQLITE_VERSIONS_DIRECTORY / f"cpython-{python_version}" / "_sqlite"
        if module_directory.is_dir():
            return module_directory
        release = ".".join(str(part) for part in sys.version_info[:3])
        source_url = CPYTHON_SOURCE_URL.format(release=release, version=python_version)
        with urllib.request.urlopen(source_url, timeout=300) as response:  # noqa: S310
            archive_bytes = response.read()
        member_prefix = f"Python-{python_version}/{CPYTHON_SQLITE_MODULE_DIRECTORY}/"
        extract_directory = module_directory.parent / "extract"
        shutil.rmtree(extract_directory, ignore_errors=True)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            members = [member for member in archive.getmembers() if member.name.startswith(member_prefix)]
            if not members:
                raise SystemExit(f"{source_url} has no {CPYTHON_SQLITE_MODULE_DIRECTORY}")
            archive.extractall(extract_directory, members=members, filter="data")
        (extract_directory / member_prefix).rename(module_directory)
        shutil.rmtree(extract_directory)
        return module_directory

    def build(self) -> Path:
        """Compiles the module into ``cp<major><minor>/`` of the version directory.

        Returns:
            The compiled module.
        """
        python_directory = self.version_directory / f"cp{sys.version_info.major}{sys.version_info.minor}"
        python_directory.mkdir(exist_ok=True)
        module_path = python_directory / f"_sqlite3{sysconfig.get_config_var('EXT_SUFFIX')}"
        if module_path.exists():
            return module_path
        module_directory = self.get_module_directory()
        include_directory = Path(sysconfig.get_paths()["include"])
        # Built for this machine's architecture only - the prepared library has no other one, while
        # a universal2 Python's own link flags ask for both.
        link_options = ["-bundle", "-undefined", "dynamic_lookup"] if sys.platform == "darwin" else ["-shared"]
        command = [
            self.compiler,
            *link_options,
            "-fPIC",
            "-O2",
            "-DPy_BUILD_CORE_MODULE",
            f"-I{include_directory}",
            f"-I{include_directory / 'internal'}",
            f"-I{self.version_directory}",
            *(str(source_path) for source_path in sorted(module_directory.glob("*.c"))),
            str(self.library_path),
            f"-Wl,-rpath,{self.version_directory}",
            "-o",
            str(module_path),
        ]
        subprocess.run(command, check=True)
        return module_path
