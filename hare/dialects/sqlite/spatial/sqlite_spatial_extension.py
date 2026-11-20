from __future__ import annotations

import sqlite3

import aiosqlite

from hare.dialects.sqlite.connection.sqlite_extension_loading import SqliteExtensionLoading
from hare.dialects.sqlite.constants import SQLITE_SPATIAL_EXTENSION_OPTION
from hare.dialects.sqlite.enums import SpatialiteMetadata
from hare.dialects.sqlite.spatial.constants import (
    SQLITE_SPATIAL_METADATA_CHECK_SQL,
    SQLITE_SPATIAL_METADATA_CREATE_SQLS,
    SQLITE_SPATIAL_PROJ_DATABASE_SQL,
    SQLITE_SPATIAL_REFERENCE_IDS_SQL,
)
from hare.exceptions import ConfigurationError


class SqliteSpatialExtension:
    """The SpatiaLite extension on a SQLite connection - its functions back ``hare.gis``'s lookups,
    functions and aggregates.

    Args:
        library_path: The library - a name the system's loader finds (``mod_spatialite``) or a path.
        proj_database: PROJ's ``proj.db`` for SRID transformations; None leaves PROJ to find its own.
        metadata: The spatial metadata created in a database without any - the reference systems a
            geography is measured on the ellipsoid of and a spatial index registers its column with.
    """

    def __init__(self, library_path: str, proj_database: str | None, metadata: SpatialiteMetadata) -> None:
        self.library_path = library_path
        self.proj_database = proj_database
        self.metadata = metadata

    @staticmethod
    def raise_if_unavailable() -> None:
        """Rejects ``load_spatialite=True`` where no extension can be loaded.

        Raises:
            ConfigurationError: This Python's sqlite3 module can't load extensions.
        """
        SqliteExtensionLoading.raise_if_unavailable(SQLITE_SPATIAL_EXTENSION_OPTION.name)

    def load(self, raw_connection: sqlite3.Connection) -> frozenset[int] | None:
        """Loads SpatiaLite into a connection, points it at ``proj_database`` and creates the spatial
        metadata when asked and missing; extension loading is switched off again after it.

        Args:
            raw_connection: The sqlite3 connection.

        Returns:
            The SRIDs of the spatial metadata's reference systems; None with ``metadata`` NONE.

        Raises:
            ConfigurationError: The library can't be loaded.
        """
        with SqliteExtensionLoading.enabled(raw_connection):
            try:
                raw_connection.load_extension(self.library_path)
            except sqlite3.OperationalError as error:
                raise ConfigurationError(f"SpatiaLite can't be loaded from {self.library_path!r}: {error}") from error
        if self.proj_database is not None:
            raw_connection.execute(SQLITE_SPATIAL_PROJ_DATABASE_SQL, (self.proj_database,))
        if self.metadata is SpatialiteMetadata.NONE:
            return None
        if not raw_connection.execute(SQLITE_SPATIAL_METADATA_CHECK_SQL).fetchone()[0]:
            raw_connection.execute(SQLITE_SPATIAL_METADATA_CREATE_SQLS[self.metadata])
        return frozenset(row[0] for row in raw_connection.execute(SQLITE_SPATIAL_REFERENCE_IDS_SQL))

    async def install(self, connection: aiosqlite.Connection) -> frozenset[int] | None:
        """Loads SpatiaLite into ``connection``.

        Returns:
            The SRIDs of the spatial metadata's reference systems; None with ``metadata`` NONE.
        """
        return await connection._execute(self.load, connection._conn)  # type: ignore[no-untyped-call]
