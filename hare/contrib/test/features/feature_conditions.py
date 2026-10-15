from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient


class FeatureConditions:
    """What a test needs of a connection - values of its ``Features``, of its dialect's
    ``SqlLiterals`` or ``Dialect``, and ``dialect`` for the dialect's name (``requires_features()``,
    the pytest marker ``hare_requires``)."""

    @staticmethod
    def get_connection(context: HareContext, connection_alias: str | None) -> DatabaseClient:
        """The connection a test's conditions are checked on.

        Args:
            context: The test's context.
            connection_alias: The connection - the context's default connection (else its first
                configured one) when None.

        Returns:
            The connection.
        """
        return context.get_connection(
            connection_alias or context.default_connection or context.connections.aliases()[0]
        )

    @staticmethod
    def get_mismatch(connection: DatabaseClient, conditions: Mapping[str, Any]) -> str | None:
        """The first condition the connection doesn't meet.

        Args:
            connection: The connection.
            conditions: Values by name.

        Returns:
            ``"<name> != <value>"``, None when every condition is met.
        """
        for name, expected in conditions.items():
            if name == "dialect":
                actual = connection.dialect.name
            elif hasattr(connection.features, name):
                actual = getattr(connection.features, name)
            elif hasattr(connection.dialect.literals, name):
                actual = getattr(connection.dialect.literals, name)
            else:
                actual = getattr(connection.dialect, name)
            if actual != expected:
                return f"{name} != {expected}"
        return None
