"""Build CPython's SQLite extension against a prepared immutable SQLite library."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

from tests.artifacts.artifact_cache import ArtifactCache
from tests.artifacts.build_spec import BuildSpec
from tests.artifacts.source_archive import SourceArchive
from tests.sqlite_versions.constants import (
    CPYTHON_SOURCE_URL,
    CPYTHON_SQLITE_MODULE_DIRECTORY,
    SQLITE_VERSIONS_DIRECTORY,
)
from tests.sqlite_versions.environment import SqliteVersionEnvironment


class SqliteModuleBuilder:
    """Prepare an ABI-specific extension without trusting a minor-version directory name."""

    def __init__(self, version: str, library_path: Path, compiler: tuple[str, ...]) -> None:
        self.version = version
        self.library_path = library_path
        self.compiler = compiler

    @staticmethod
    def get_module_directory() -> Path:
        """Extract the running Python's C sources through the same verified artifact cache."""
        python_version = platform.python_version()
        release = ".".join(str(part) for part in sys.version_info[:3])
        url = CPYTHON_SOURCE_URL.format(release=release, version=python_version)
        archive_path = SourceArchive.get(url, SQLITE_VERSIONS_DIRECTORY / "sources")
        spec = BuildSpec.create(f"cpython-{python_version}-sqlite-sources", {url: archive_path})
        prefix = f"Python-{python_version}/{CPYTHON_SQLITE_MODULE_DIRECTORY}/"

        def build(stage: Path) -> None:
            with tempfile.TemporaryDirectory(dir=stage.parent) as temporary:
                with tarfile.open(archive_path, mode="r:gz") as archive:
                    members = [member for member in archive.getmembers() if member.name.startswith(prefix)]
                    if not members:
                        raise SystemExit(f"{url} has no {CPYTHON_SQLITE_MODULE_DIRECTORY}")
                    archive.extractall(temporary, members=members, filter="data")
                shutil.copytree(Path(temporary) / prefix, stage, dirs_exist_ok=True)

        def verify(stage: Path) -> None:
            if not (stage / "module.c").is_file():
                raise SystemExit(f"{url} has no SQLite module source")

        return ArtifactCache(SQLITE_VERSIONS_DIRECTORY / "cpython-sources").prepare(spec, build, verify)

    def build(self) -> Path:
        """Compile in staging, check import and version, then publish the extension directory."""
        module_directory = self.get_module_directory()
        include_directory = Path(sysconfig.get_paths()["include"])
        library_directory = self.library_path.parent
        extension_name = f"_sqlite3{sysconfig.get_config_var('EXT_SUFFIX')}"
        options = [
            "-shared",
            "-fPIC",
            "-O2",
            "-DPy_BUILD_CORE_MODULE",
            f"-I{include_directory}",
            f"-I{include_directory / 'internal'}",
            f"-I{library_directory}",
            f"-Wl,-rpath,{library_directory}",
        ]
        sources = {
            path.relative_to(module_directory).as_posix(): path
            for path in module_directory.rglob("*")
            if path.is_file()
        }
        sources.update({"library": self.library_path, "sqlite3.h": library_directory / "sqlite3.h"})
        config_header = include_directory / "pyconfig.h"
        if config_header.is_file():
            sources["pyconfig.h"] = config_header
        spec = BuildSpec.create(
            f"sqlite-{self.version}-python", sources, compiler=self.compiler, options=options, python_abi=True
        )

        def build(stage: Path) -> None:
            subprocess.run(
                [
                    *self.compiler,
                    *options,
                    *(str(path) for path in sorted(module_directory.glob("*.c"))),
                    str(self.library_path),
                    "-o",
                    str(stage / extension_name),
                ],
                check=True,
            )

        environment = SqliteVersionEnvironment(self.version)
        directory = ArtifactCache(SQLITE_VERSIONS_DIRECTORY / self.version / "artifacts").prepare(
            spec, build, lambda stage: environment.verify(library_directory, stage)
        )
        return directory / extension_name
