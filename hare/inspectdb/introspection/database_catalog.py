from __future__ import annotations

from typing import TYPE_CHECKING

from hare.inspectdb.exceptions import UnsupportedDialectError

if TYPE_CHECKING:
    from hare.ddl.schema_objects.database_function import DatabaseFunction
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
    from hare.inspectdb.introspection.schema_part import SchemaPart
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
    async def get_views(
        connection: DatabaseClient, schema: str | None = None
    ) -> list[tuple[str, bool, tuple[str, ...]]]:
        """Lists the views of a schema - which inspectdb makes no model of.

        Args:
            connection: The connection.
            schema: The schema, the connection's default one when None.

        Returns:
            Each view's name, whether it is a materialized one, and the names of the triggers on it.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        return await introspector_class.fetch_views(connection, schema)

    @staticmethod
    async def get_schema_object_notes(connection: DatabaseClient, schema: str | None = None) -> list[str]:
        """Lists the notes naming the schema objects of a schema inspectdb declares nothing of.

        Args:
            connection: The connection.
            schema: The schema, the connection's default one when None.

        Returns:
            One comment line per object.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        return await introspector_class.fetch_schema_object_notes(connection, schema)

    @staticmethod
    async def get_unattached_schema_objects(
        connection: DatabaseClient, schema: str | None = None
    ) -> tuple[list[DatabaseSequence], list[DatabaseFunction], list[SchemaPart]]:
        """Lists the sequences and functions of a schema no table holds, and the grants on them.

        Args:
            connection: The connection.
            schema: The schema, the connection's default one when None.

        Returns:
            The sequences, the functions and the grants.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        return await introspector_class.fetch_unattached_schema_objects(connection, schema)

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
    async def read_tables(connection: DatabaseClient, tables: list[str], schema: str | None = None) -> CatalogSnapshot:
        """Read an immutable catalog snapshot without reconstructing fields or models.

        Records are dialect-specific and preserve unknown metadata. The connection's
        transaction controls consistency across the reader's catalog statements.

        Args:
            connection: The connection.
            tables: The tables to capture, preserving their order and repeated names.
            schema: The tables' namespace, the connection's default one when None.

        Returns:
            The catalog records for the requested tables. An empty request does not
            check whether the schema exists.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: The requested tables' schema doesn't exist.
            TableNotFoundError: A requested table doesn't exist in the schema.
        """
        introspector = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector.fetch_default_schema(connection)
        return await introspector.fetch_catalog(connection, tables, schema)

    @staticmethod
    async def inspect_table(connection: DatabaseClient, table: str, schema: str | None = None) -> TableInfo:
        """Inspects one table's full schema.

        Args:
            connection: The connection.
            table: The table.
            schema: The schema ``table`` lives in (a schema, an attached database or a database of
                the server - the namespace of tables the database has), the connection's default one when None.

        Returns:
            The table's schema.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: The table's schema doesn't exist.
            TableNotFoundError: If ``table`` doesn't exist.
        """
        table_infos = await DatabaseCatalog.inspect_tables(connection, [table], schema=schema)
        return table_infos[0]

    @staticmethod
    async def inspect_tables(
        connection: DatabaseClient, tables: list[str], schema: str | None = None
    ) -> list[TableInfo]:
        """Inspects several tables' full schemas.

        Args:
            connection: The connection.
            tables: The tables to inspect.
            schema: The schema the tables live in (a schema, an attached database or a database of
                the server - the namespace of tables the database has), the connection's default one when None.

        Returns:
            One TableInfo per entry of ``tables``, in the same order. An empty
            request does not check whether the schema exists.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: The requested tables' schema doesn't exist.
            TableNotFoundError: If a table doesn't exist - checked explicitly, since a catalog
                that silently returns nothing for one would otherwise describe a table with no
                columns at all.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        return await introspector_class.fetch_tables(connection, tables, schema)
