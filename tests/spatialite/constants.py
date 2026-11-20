from __future__ import annotations

#: The environment variables of the SpatiaLite tests: the library (a name the system's loader finds,
#: as ``mod_spatialite`` of Linux's ``libsqlite3-mod-spatialite``, or a path) and PROJ's proj.db
#: (optional). Without the library the SpatiaLite tests skip.
SPATIALITE_PATH_ENVIRONMENT_VARIABLE = "HARE_TEST_SPATIALITE_PATH"
SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE = "HARE_TEST_SPATIALITE_PROJ_DATABASE"
