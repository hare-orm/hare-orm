from __future__ import annotations

import os
import sqlite3
import urllib.parse

import pytest

from hare.dialects.sqlite.enums import SpatialiteMetadata
from tests.spatialite.constants import (
    SPATIALITE_PATH_ENVIRONMENT_VARIABLE,
    SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE,
)


class SpatialiteTestSettings:
    """The SQLite settings of the SpatiaLite tests, from their environment variables."""

    @staticmethod
    def get_options(*, metadata: SpatialiteMetadata = SpatialiteMetadata.WGS84) -> dict[str, str]:
        """The connection settings loading SpatiaLite - the test skips when no library is set.

        Args:
            metadata: The spatial metadata created - a geography and a spatial index need some.

        Returns:
            The settings.
        """
        library_path = os.environ.get(SPATIALITE_PATH_ENVIRONMENT_VARIABLE)
        if not library_path:
            pytest.skip(f"SpatiaLite isn't set up - {SPATIALITE_PATH_ENVIRONMENT_VARIABLE} names no library")
        if not hasattr(sqlite3.Connection, "enable_load_extension"):
            pytest.skip("this Python's sqlite3 module loads no extension")
        options = {
            "load_spatialite": "true",
            "spatialite_path": library_path,
            "spatialite_metadata": metadata.value.lower(),
        }
        proj_database = os.environ.get(SPATIALITE_PROJ_DATABASE_ENVIRONMENT_VARIABLE)
        if proj_database:
            options["spatialite_proj_database"] = proj_database
        return options

    @staticmethod
    def get_db_url(*, metadata: SpatialiteMetadata = SpatialiteMetadata.WGS84) -> str:
        """An in-memory SQLite database loading SpatiaLite - the test skips when no library is set.

        Args:
            metadata: The spatial metadata created - a geography and a spatial index need some.

        Returns:
            The URL.
        """
        options = SpatialiteTestSettings.get_options(metadata=metadata)
        return f"sqlite+aiosqlite://:memory:?{urllib.parse.urlencode(options)}"
