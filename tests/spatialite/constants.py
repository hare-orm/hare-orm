from __future__ import annotations

from pathlib import Path

from tests.matrix.matrix_catalog import MatrixCatalog

#: Where the downloaded SpatiaLite is kept.
SPATIALITE_DIRECTORY = Path(__file__).parent

#: The official SpatiaLite build for 64-bit Windows - the library with GEOS, PROJ and its proj.db.
SPATIALITE_WINDOWS_ARCHIVE_URL = MatrixCatalog.get("spatialite.windows_archive_url")
SPATIALITE_VERSION = MatrixCatalog.get("spatialite.version")
#: The directory the archive unpacks into.
SPATIALITE_WINDOWS_DIRECTORY_NAME = f"mod_spatialite-{SPATIALITE_VERSION}-win-amd64"

#: The environment variables of the SpatiaLite tests: the library (a name the system's loader finds,
#: as ``mod_spatialite`` of Linux's ``libsqlite3-mod-spatialite``, or a path) and PROJ's proj.db
#: (optional). Without the library the SpatiaLite tests skip.
SPATIALITE_PATH_ENVIRONMENT_VARIABLE = "HARE_TEST_SPATIALITE_PATH"
SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE = "HARE_TEST_SPATIALITE_PROJ_DATABASE"
