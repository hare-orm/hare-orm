from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.gis.constants import SPATIAL_LOOKUP_REQUIRED_FEATURE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.expressions import ExpressionContext


class SpatialFeatures:
    """Checks a connection runs ``hare.gis``'s spatial SQL before a query is sent."""

    @staticmethod
    def raise_if_unsupported(
        expression_context: ExpressionContext, name: str, feature: str = SPATIAL_LOOKUP_REQUIRED_FEATURE
    ) -> None:
        """Rejects a spatial function or aggregate on a connection without spatial SQL.

        Args:
            expression_context: The context the expression is resolved in.
            name: The function or aggregate, named in the error.
            feature: The ``Features`` flag it needs - ``supports_spatial``, or ``supports_geography`` for
                a geography.

        Raises:
            UnSupportedError: The connection hasn't the flag.
        """
        connection = expression_context.connection
        if connection is not None and not getattr(connection.features, feature):
            raise UnSupportedError(
                f"{name} can't run on the {connection.connection_alias!r} connection: it needs features.{feature}, "
                "which the connection doesn't have"
            )

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
