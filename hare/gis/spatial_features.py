from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class SpatialFeatures:
    """Check the spatial reference systems available to a connection."""

    @staticmethod
    def raise_if_unknown_reference_system(connection: DatabaseClient, name: str, srid: int) -> None:
        """Rejects an SRID the database's spatial metadata hasn't - a geography is measured on its
        reference system's ellipsoid, a spatial index registers its column with it.

        Args:
            connection: The connection.
            name: What needs the reference system, named in the error.
            srid: The SRID.

        Raises:
            UnSupportedError: The connection reads its reference systems from spatial metadata without
                the SRID.
        """
        spatial_reference_ids = connection.features.spatial_reference_ids
        if spatial_reference_ids is not None and srid not in spatial_reference_ids:
            raise UnSupportedError(
                f"{name} can't run on the {connection.connection_alias!r} connection: its spatial metadata has no "
                f"reference system {srid} - create the metadata with every reference system, or add this one to it"
            )
