"""Downloads SpatiaLite for the SpatiaLite tests on Windows into ``tests/spatialite/`` and prints the
environment variables pointing the tests at it. Linux and macOS install it from their packages
(``apt-get install libsqlite3-mod-spatialite``, ``brew install libspatialite``) and set
``HARE_TEST_SPATIALITE_PATH=mod_spatialite``.

Usage:
    python -m tests.spatialite.download_spatialite
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.artifacts.artifact_cache import ArtifactCache
from tests.artifacts.build_spec import BuildSpec
from tests.artifacts.source_archive import SourceArchive
from tests.spatialite.constants import (
    SPATIALITE_DIRECTORY,
    SPATIALITE_PATH_ENVIRONMENT_VARIABLE,
    SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE,
    SPATIALITE_VERSION,
    SPATIALITE_WINDOWS_ARCHIVE_URL,
    SPATIALITE_WINDOWS_DIRECTORY_NAME,
)


class SpatialiteDownloader:
    """Downloads and unpacks the Windows build of SpatiaLite."""

    #: Where 7-Zip is installed when it isn't on PATH.
    SEVEN_ZIP_INSTALLED_PATH = Path("C:/Program Files/7-Zip/7z.exe")

    def run(self) -> None:
        if sys.platform != "win32":
            raise SystemExit(
                "Install SpatiaLite from the system's packages (apt-get install libsqlite3-mod-spatialite, "
                f"brew install libspatialite) and set {SPATIALITE_PATH_ENVIRONMENT_VARIABLE}=mod_spatialite"
            )
        archive_path = SourceArchive.get(SPATIALITE_WINDOWS_ARCHIVE_URL, SPATIALITE_DIRECTORY / "sources")
        spec = BuildSpec.create(f"spatialite-{SPATIALITE_VERSION}", {SPATIALITE_WINDOWS_ARCHIVE_URL: archive_path})

        def build(stage: Path) -> None:
            with tempfile.TemporaryDirectory(dir=stage.parent) as temporary:
                subprocess.run(
                    [self.get_seven_zip(), "x", "-t7z", "-y", f"-o{temporary}", str(archive_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                shutil.copytree(Path(temporary) / SPATIALITE_WINDOWS_DIRECTORY_NAME, stage, dirs_exist_ok=True)

        library_directory = ArtifactCache(SPATIALITE_DIRECTORY / "artifacts").prepare(spec, build, self.verify)
        print(f"{SPATIALITE_PATH_ENVIRONMENT_VARIABLE}={library_directory / 'mod_spatialite'}")
        print(f"{SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE}={library_directory / 'proj.db'}")

    @staticmethod
    def verify(directory: Path) -> None:
        """Load the extension with its bundled DLLs before publishing paths for the tests."""
        if not (directory / "proj.db").is_file():
            raise SystemExit("SpatiaLite's archive has no proj.db")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, sqlite3, sys; directory = sys.argv[1]; "
                "dlls = os.add_dll_directory(directory); connection = sqlite3.connect(':memory:'); "
                "connection.enable_load_extension(True); "
                "connection.load_extension(os.path.join(directory, 'mod_spatialite')); "
                "print(connection.execute('SELECT spatialite_version()').fetchone()[0]); "
                "connection.close(); dlls.close()",
                str(directory),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip() != SPATIALITE_VERSION:
            raise SystemExit(f"SpatiaLite reports {result.stdout.strip()}, expected {SPATIALITE_VERSION}")

    def get_seven_zip(self) -> str:
        """The 7-Zip program the archive is unpacked with.

        Raises:
            SystemExit: 7-Zip isn't installed.
        """
        seven_zip = shutil.which("7z") or (
            str(self.SEVEN_ZIP_INSTALLED_PATH) if self.SEVEN_ZIP_INSTALLED_PATH.exists() else None
        )
        if seven_zip is None:
            raise SystemExit("7-Zip (7z) is needed to unpack SpatiaLite's archive")
        return seven_zip


if __name__ == "__main__":
    SpatialiteDownloader().run()
