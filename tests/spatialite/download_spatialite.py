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
import urllib.request
from pathlib import Path

from tests.spatialite.constants import (
    SPATIALITE_DIRECTORY,
    SPATIALITE_PATH_ENVIRONMENT_VARIABLE,
    SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE,
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
        library_directory = SPATIALITE_DIRECTORY / SPATIALITE_WINDOWS_DIRECTORY_NAME
        if not (library_directory / "mod_spatialite.dll").exists():
            archive_path = SPATIALITE_DIRECTORY / f"{SPATIALITE_WINDOWS_DIRECTORY_NAME}.7z"
            with urllib.request.urlopen(SPATIALITE_WINDOWS_ARCHIVE_URL) as response:  # noqa: S310  # nosec B310
                archive_path.write_bytes(response.read())
            subprocess.run(  # noqa: S603  # nosec B603
                [self.get_seven_zip(), "x", "-y", f"-o{SPATIALITE_DIRECTORY}", str(archive_path)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            archive_path.unlink()
        print(f"{SPATIALITE_PATH_ENVIRONMENT_VARIABLE}={library_directory / 'mod_spatialite'}")
        print(f"{SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE}={library_directory / 'proj.db'}")

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
