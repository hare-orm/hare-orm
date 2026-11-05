"""Downloads the SQLite versions the test matrix runs against and prepares them for this Python.

Each version keeps immutable artifacts below ``tests/sqlite_versions/<version>/artifacts``.
Their manifests record source digests, platform, compiler, options and the Python ABI where relevant.
A minor-version selection file points to the successfully verified library and extension artifacts.
Windows uses the official DLL beside this Python's extension; Linux/macOS build the library and,
when the Python extension cannot load it, an extension from the matching CPython release sources.

``tests/sqlite_versions/environment.py`` prints the environment variables of one version.

Usage:
    python -m tests.sqlite_versions.download_sqlite_versions [VERSION ...] - "newest" names the newest one
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

from tests.artifacts.artifact_cache import ArtifactCache
from tests.artifacts.build_spec import BuildSpec
from tests.artifacts.source_archive import SourceArchive
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
            environment = SqliteVersionEnvironment(version)
            library_directory = python_directory = None
            if sys.platform == "win32":
                python_directory = self.prepare_windows(version, version_directory)
            else:
                library_path = self.build_library(version, version_directory)
                library_directory = library_path.parent
                try:
                    needs_module = environment.get_loaded_version(library_directory, None) != version
                except subprocess.CalledProcessError:
                    needs_module = True
                if needs_module:
                    module_path = SqliteModuleBuilder(version, library_path, self.get_compiler()).build()
                    python_directory = module_path.parent
            environment.verify(library_directory, python_directory)
            environment.publish(library_directory, python_directory)
            print(f"SQLite {version}: {python_directory or library_directory}")

    @staticmethod
    def prepare_windows(version: str, version_directory: Path) -> Path:
        """Publish the official DLL with this interpreter's extension only after an import check."""
        url = f"{SQLITE_DOWNLOAD_ROOT}/{SQLITE_RELEASES[version]['windows_dll']}"
        archive_path = SourceArchive.get(url, SQLITE_VERSIONS_DIRECTORY / "sources")
        extension_path = Path(sys.base_prefix) / "DLLs" / "_sqlite3.pyd"
        spec = BuildSpec.create(
            f"sqlite-{version}-windows", {url: archive_path, "_sqlite3.pyd": extension_path}, python_abi=True
        )

        def build(stage: Path) -> None:
            with zipfile.ZipFile(archive_path) as archive:
                (stage / "sqlite3.dll").write_bytes(archive.read("sqlite3.dll"))
            shutil.copyfile(extension_path, stage / "_sqlite3.pyd")

        environment = SqliteVersionEnvironment(version)
        return ArtifactCache(version_directory / "artifacts").prepare(
            spec, build, lambda stage: environment.verify(None, stage)
        )

    @staticmethod
    def get_compiler() -> tuple[str, ...]:
        """The C compiler this Python was built with, ``cc`` when that one isn't on this machine.

        Returns:
            The compiler's executable.
        """
        compiler = tuple(shlex.split(sysconfig.get_config_var("CC") or "cc"))
        # Relocated builds (python-build-standalone, which uv installs) name the compiler of the
        # machine they were built on.
        return compiler if shutil.which(compiler[0]) else ("cc",)

    @classmethod
    def build_library(cls, version: str, version_directory: Path) -> Path:
        """Build an immutable library/header artifact from one recorded amalgamation archive."""
        library_name = "libsqlite3.0.dylib" if sys.platform == "darwin" else "libsqlite3.so.0"
        url = f"{SQLITE_DOWNLOAD_ROOT}/{SQLITE_RELEASES[version]['amalgamation']}"
        archive_path = SourceArchive.get(url, SQLITE_VERSIONS_DIRECTORY / "sources")
        compiler = cls.get_compiler()
        options = ["-shared", "-fPIC", "-O2", *SQLITE_COMPILE_OPTIONS]
        link_options = ["-lpthread", "-lm"]
        link_options.extend(
            ["-install_name", f"@rpath/{library_name}"]
            if sys.platform == "darwin"
            else ["-ldl", f"-Wl,-soname,{library_name}"]
        )
        spec = BuildSpec.create(
            f"sqlite-{version}-library", {url: archive_path}, compiler=compiler, options=[*options, *link_options]
        )

        def build(stage: Path) -> None:
            with zipfile.ZipFile(archive_path) as archive:
                for file_name in ("sqlite3.c", "sqlite3.h"):
                    member = next(name for name in archive.namelist() if name.endswith(f"/{file_name}"))
                    (stage / file_name).write_bytes(archive.read(member))
            subprocess.run(
                [*compiler, *options, str(stage / "sqlite3.c"), "-o", str(stage / library_name), *link_options],
                check=True,
            )
            (stage / "sqlite3.c").unlink()

        def verify(stage: Path) -> None:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import ctypes, sys; library = ctypes.CDLL(sys.argv[1]); "
                    "library.sqlite3_libversion.restype = ctypes.c_char_p; "
                    "print(library.sqlite3_libversion().decode())",
                    str(stage / library_name),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            if result.stdout.strip() != version:
                raise SystemExit(f"The built SQLite library reports {result.stdout.strip()}, expected {version}")

        return ArtifactCache(version_directory / "artifacts").prepare(spec, build, verify) / library_name


if __name__ == "__main__":
    SqliteVersionDownloader(sys.argv[1:]).run()
