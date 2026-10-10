from __future__ import annotations

from typing import TYPE_CHECKING

from hare.inspectdb.exceptions import TableNotFoundError, UnsupportedDialectError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
    from hare.inspectdb.introspection.table_info import TableInfo


class DatabaseCatalog:
    """What a connection's database holds - asked of the introspector of the connection's dialect
    (``SchemaIntrospector``): its schemas, its tables and their full definitions."""

    @staticmethod
    def get_dialect_introspector_class(dialect_name: str | Dialect) -> type[SchemaIntrospector]:
        """Returns the introspector of a dialect.

        Args:
            dialect_name: The dialect's name.

        Returns:
            The dialect's ``SchemaIntrospector`` subclass.

        Raises:
            UnsupportedDialectError: The dialect has no introspector.
        """
        # Local import: the registry's dialects import this module.
        from hare.dialects.dialect_registry import DialectRegistry

        dialect = DialectRegistry.get_dialect(dialect_name)
        if dialect.introspector_class is None:
            raise UnsupportedDialectError(dialect.name)
        return dialect.introspector_class

    @staticmethod
    def get_introspector_class(connection: DatabaseClient) -> type[SchemaIntrospector]:
        """Returns the introspector of a connection's dialect.

        Args:
            connection: The connection.

        Returns:
            The dialect's ``SchemaIntrospector`` subclass.

        Raises:
            UnsupportedDialectError: The dialect has no introspector.
        """
        introspector_class = connection.dialect.introspector_class
        if introspector_class is None:
            raise UnsupportedDialectError(connection.dialect.name)
        return introspector_class

    @staticmethod
    async def get_default_schema(connection: DatabaseClient) -> str:
        """The schema an unqualified CREATE TABLE lands in.

        Args:
            connection: The connection.

        Returns:
            The schema's name.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        return await DatabaseCatalog.get_introspector_class(connection).fetch_default_schema(connection)

    @staticmethod
    async def get_schema_names(connection: DatabaseClient) -> list[str]:
        """The names of the database's schemas.

        Args:
            connection: The connection.

        Returns:
            The names, sorted.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            UnSupportedError: The database has no schemas.
        """
        return await DatabaseCatalog.get_introspector_class(connection).fetch_schema_names(connection)

    @staticmethod
    async def get_table_names(
        connection: DatabaseClient, schema: str | None = None, *, include_partitions: bool = False
    ) -> list[str]:
        """Lists every table name in the given schema.

        Args:
            connection: The connection.
            schema: The schema to list tables from, the connection's default one (the schema an
                unqualified CREATE TABLE lands in) when None - ignored by a dialect without
                schemas. Every table in another schema is otherwise invisible to inspectdb.
            include_partitions: Whether to list the partitions of a partitioned table too -
                otherwise its partitioned parent table stands for them.

        Returns:
            The table names.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: The schema doesn't exist.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        return await introspector_class.fetch_table_names(connection, schema, include_partitions)

    @staticmethod
    async def table_exists(connection: DatabaseClient, table: str) -> bool:
        """Returns whether a table exists in the connection's default schema.

        Args:
            connection: The connection.
            table: The table's name.

        Returns:
            Whether it exists.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        return await DatabaseCatalog.get_introspector_class(connection).fetch_table_exists(connection, table)

    @staticmethod
    async def inspect_table(
        connection: DatabaseClient, table: str, schema: str | None = None, *, verify_exists: bool = True
    ) -> TableInfo:
        """Inspects one table's full schema.

        Args:
            connection: The connection.
            table: The table.
            schema: The schema ``table`` lives in, the connection's default one when None -
                ignored by a dialect without schemas.
            verify_exists: See inspect_tables().

        Returns:
            The table's schema.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            TableNotFoundError: If ``table`` doesn't exist.
        """
        table_infos = await DatabaseCatalog.inspect_tables(
            connection, [table], schema=schema, verify_exists=verify_exists
        )
        return table_infos[0]

    @staticmethod
    async def inspect_tables(
        connection: DatabaseClient, tables: list[str], schema: str | None = None, *, verify_exists: bool = True
    ) -> list[TableInfo]:
        """Inspects several tables' full schemas.

        Args:
            connection: The connection.
            tables: The tables to inspect.
            schema: The schema the tables live in, the connection's default one when None -
                ignored by a dialect without schemas.
            verify_exists: Set to ``False`` only when the caller already matched ``tables``
                against a table list it just fetched itself - skips this method's own
                get_table_names() round trip. A dialect whose catalog statements take no bind
                parameters relies on that check to make a table name safe in them.

        Returns:
            One TableInfo per entry of ``tables``, in the same order.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            TableNotFoundError: If a table doesn't exist - checked explicitly, since a catalog
                that silently returns nothing for one would otherwise describe a table with no
                columns at all.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        if verify_exists and tables:
            existing_table_names = set(await introspector_class.fetch_table_names(connection, schema, True))
            for table in tables:
                if table not in existing_table_names:
                    raise TableNotFoundError(table)
        return await introspector_class.fetch_tables(connection, tables, schema)
