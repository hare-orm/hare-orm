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
            client: The database client whose schema and type settings are used.
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The DDL script.
        """
        return client.dialect.schema_editor_class(client).table_creation.get_create_schema_sql(safe)

    @classmethod
    async def generate_schema(cls, client: DatabaseClient, safe: bool) -> None:
        """Creates every model of this connection in the database.

        Args:
            client: The database client whose schema and type settings are used.
            safe: Whether each object is created only when it doesn't exist yet.
        """
        schema = await cls.build_connected_sql(client, safe)
        await cls.execute_schema(client, schema)

    @staticmethod
    async def build_connected_sql(client: DatabaseClient, safe: bool) -> str:
        """Render schema SQL using the connected server's capabilities."""
        async with client.acquire_connection():
            pass
        return client.get_schema_sql(safe)

    @staticmethod
    async def execute_schema(client: DatabaseClient, schema: str) -> None:
        """Execute schema SQL and invalidate the affected client caches."""
        client.log.debug("Creating schema: %s", schema)
        if schema:
            async with SchemaCacheInvalidation.run(client):
                await client.execute_built_script(schema)
