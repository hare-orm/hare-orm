from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.client.schema_cache_invalidation import SchemaCacheInvalidation

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class ClientSchema:
    """Builds and executes schema creation for the models assigned to a database client."""

    @staticmethod
    def get_schema_sql(client: DatabaseClient, safe: bool) -> str:
        """The DDL creating every model of this connection.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The DDL script.
        """
        return client.dialect.schema_editor_class(client).table_creation.get_create_schema_sql(safe)

    @staticmethod
    async def generate_schema(client: DatabaseClient, safe: bool) -> None:
        """Creates every model of this connection in the database.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.
        """
        # Connected first, so the DDL is written for the features of the server's own version.
        async with client.acquire_connection():
            pass
        schema = client.get_schema_sql(safe)
        client.log.debug("Creating schema: %s", schema)
        if schema:
            async with SchemaCacheInvalidation.run(client):
                await client.execute_built_script(schema)
