from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_COLUMNS_SQL,
    CLICKHOUSE_CURRENT_DATABASE_SQL,
    CLICKHOUSE_DEFAULT_INDEX_TYPE,
    CLICKHOUSE_GENERATED_COLUMN_TYPES,
    CLICKHOUSE_INDEX_TYPE_ARGUMENTS,
    CLICKHOUSE_INDEXES_SQL,
    CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES,
    CLICKHOUSE_TABLE_EXISTS_SQL,
    CLICKHOUSE_TABLE_NAMES_SQL,
    CLICKHOUSE_TABLE_SQL,
    CLICKHOUSE_TYPE_MAP,
    CLICKHOUSE_TYPE_WRAPPERS,
    CLICKHOUSE_VIRTUAL_COLUMN_TYPE,
)
from hare.dialects.clickhouse.indexes import (
    BloomFilterIndex,
    MinMaxIndex,
    NgramBloomFilterIndex,
    SetIndex,
    TokenBloomFilterIndex,
)
from hare.dialects.clickhouse.introspection.clickhouse_declared_dictionaries import ClickhouseDeclaredDictionaries
from hare.dialects.clickhouse.introspection.clickhouse_declared_table_options import ClickhouseDeclaredTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_declared_views import ClickhouseDeclaredViews
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.clickhouse_type_parser import ClickhouseTypeParser
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_ENUM_LABEL_FIELD_PATH
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseIntrospector(SchemaIntrospector):
    """Reads a ClickHouse schema from the ``system`` tables - a database is the namespace of the
    tables, its columns' types are ClickHouse's own."""

    TYPE_MAP: ClassVar[list[tuple[str, str, dict[str, Any]]]] = CLICKHOUSE_TYPE_MAP
    INDEX_CLASSES_BY_TYPE = {
        "minmax": MinMaxIndex,
        "set": SetIndex,
        "bloom_filter": BloomFilterIndex,
        "ngrambf_v1": NgramBloomFilterIndex,
        "tokenbf_v1": TokenBloomFilterIndex,
    }
    TUNED_INDEX_TYPES = frozenset(CLICKHOUSE_INDEX_TYPE_ARGUMENTS)

    @staticmethod
    def get_index_storage_parameters(index_type: str, type_sql: str, granularity: int) -> dict[str, str]:
        """The arguments of a data skipping index as its class takes them.

        Args:
            index_type: The index's type - ``set``.
            type_sql: The type with its arguments - ``set(100)``.
            granularity: The index's granularity.

        Returns:
            The text of each argument by its name, with the granularity.
        """
        _, arguments = ClickhouseTypeNames.get_type_parts(type_sql)
        argument_names = CLICKHOUSE_INDEX_TYPE_ARGUMENTS.get(index_type, ())
        return {"granularity": str(granularity), **dict(zip(argument_names, arguments, strict=False))}

    @classmethod
    async def fetch_default_schema(cls, connection: DatabaseClient) -> str:
        rows = await connection.execute_dicts(CLICKHOUSE_CURRENT_DATABASE_SQL)
        return str(rows[0]["name"])

    @classmethod
    async def fetch_table_names(cls, connection: DatabaseClient, schema: str, include_partitions: bool) -> list[str]:
        return [row["name"] for row in await connection.execute_dicts(CLICKHOUSE_TABLE_NAMES_SQL, [schema])]

    @classmethod
    async def fetch_table_exists(cls, connection: DatabaseClient, table: str) -> bool:
        return bool(await connection.execute_dicts(CLICKHOUSE_TABLE_EXISTS_SQL, [table]))

    @classmethod
    async def fetch_tables(cls, connection: DatabaseClient, tables: list[str], schema: str) -> list[TableInfo]:
        return [await cls.fetch_table(connection, table, schema) for table in tables]

    @classmethod
    async def fetch_table(cls, connection: DatabaseClient, table: str, schema: str) -> TableInfo:
        """Reads one table: its columns, primary key, data skipping indexes, storage options and
        comment.

        Args:
            connection: The connection.
            table: The table.
            schema: The database it lives in.

        Returns:
            The table.
        """
        table_rows = await connection.execute_dicts(CLICKHOUSE_TABLE_SQL, [schema, table])
        column_rows = await connection.execute_dicts(CLICKHOUSE_COLUMNS_SQL, [schema, table])
        column_names = {str(row["name"]) for row in column_rows}
        # A key of the primary key that is no column - the expression a sample is picked by - is none
        # of a model's key.
        primary_key_columns = [
            identifier
            for key_sql in ClickhouseSqlParts.split(str(table_rows[0]["primary_key"]) if table_rows else "")
            if (identifier := ClickhouseSqlParts.get_identifier(key_sql)) in column_names
        ]
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        for row in await connection.execute_dicts(CLICKHOUSE_INDEXES_SQL, [schema, table]):
            expression = str(row["expr"])
            # An index over columns lists them, comma-separated; any other expression is kept whole.
            parts = [part.strip() for part in expression.split(",")]
            index_columns = parts if all(part in column_names for part in parts) else [expression]
            # A minmax index of granularity 1 is the one hare writes for an index of no type. Over one
            # column it is a field flag, as a plain single-column index is on the other databases.
            index_type = str(row["type"])
            granularity = int(row["granularity"])
            is_default_type = index_type == CLICKHOUSE_DEFAULT_INDEX_TYPE and granularity == 1
            if is_default_type and len(index_columns) == 1 and expression in column_names:
                column_indexes.append(IndexInfo(columns=index_columns, is_unique=False, name=str(row["name"])))
            else:
                indexes.append(
                    IndexInfo(
                        columns=index_columns,
                        is_unique=False,
                        name=str(row["name"]),
                        index_type="" if is_default_type else index_type,
                        storage_parameters={}
                        if is_default_type
                        else cls.get_index_storage_parameters(index_type, str(row["type_full"]), granularity),
                    )
                )
        indexed_columns = {index.columns[0] for index in column_indexes}
        columns = []
        for row in column_rows:
            db_type, nullable = cls.unwrap_type(str(row["type"]))
            precision, scale = cls.get_decimal_precision_scale(db_type)
            name = str(row["name"])
            columns.append(
                ColumnInfo(
                    name=name,
                    db_type=db_type,
                    nullable=nullable,
                    is_pk=name in primary_key_columns,
                    is_unique=False,
                    pk_position=primary_key_columns.index(name) + 1 if name in primary_key_columns else None,
                    numeric_precision=precision,
                    numeric_scale=scale,
                    db_default=cls.parse_db_default(str(row["default_expression"]))
                    if row["default_type"] == "DEFAULT"
                    else None,
                    generated_expression=str(row["default_expression"])
                    if row["default_type"] in CLICKHOUSE_GENERATED_COLUMN_TYPES
                    else None,
                    generated_stored=row["default_type"] != CLICKHOUSE_VIRTUAL_COLUMN_TYPE,
                    description=str(row["comment"]) or None,
                    full_type=str(row["type"]),
                    has_index=name in indexed_columns,
                )
            )
        return TableInfo(
            name=table,
            schema=None,
            columns=columns,
            indexes=indexes,
            column_indexes=column_indexes,
            table_description=(str(table_rows[0]["comment"]) or None) if table_rows else None,
            table_options=await ClickhouseObservedTableOptions.fetch(
                connection, table_rows[0], column_rows, primary_key_columns
            )
            if table_rows
            else None,
        )

    @classmethod
    async def fetch_declared_table_options(
        cls,
        connection: DatabaseClient,
        observed: TableOptions | None,
        declared: TableOptions | None,
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> TableOptions | None:
        """What the declared table options are compared as: each declared option stands where the
        table's own means the same - an expression the server writes its own way, a codec it gave its
        arguments, a setting holding the server's value, a sort by the primary key."""
        return await ClickhouseDeclaredTableOptions.fetch(
            connection,
            observed if isinstance(observed, ClickhouseTableOptions) else None,
            declared if isinstance(declared, ClickhouseTableOptions) else None,
            table_info,
            column_to_field_name,
        )

    @classmethod
    async def fetch_declared_schema_objects(
        cls,
        connection: DatabaseClient,
        schema: str,
        declared: Mapping[ModelOption, tuple[Any, ...]],
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> Mapping[ModelOption, tuple[Any, ...]]:
        views, materialized_views = await ClickhouseDeclaredViews.fetch(
            connection,
            schema,
            declared.get(ModelOption.VIEWS, ()),
            declared.get(ModelOption.MATERIALIZED_VIEWS, ()),
        )
        dictionaries = await ClickhouseDeclaredDictionaries.fetch(
            connection, schema, declared.get(ModelOption.DICTIONARIES, ()), table_info, column_to_field_name
        )
        return {
            ModelOption.VIEWS: views,
            ModelOption.MATERIALIZED_VIEWS: materialized_views,
            ModelOption.DICTIONARIES: dictionaries,
        }

    @classmethod
    def column_types_differ(cls, declared_type: str, column: ColumnInfo) -> bool:
        """Whether a column's type is not the one a field declares - both named as the server names
        them (``VARCHAR(10)`` is ``String``), the column's own ``Nullable`` left out (its NULLs are a
        flag of their own), spaces ignored.

        Args:
            declared_type: The field's type.
            column: The introspected column.

        Returns:
            Whether they differ.
        """
        observed_type = ClickhouseTypeNames.get_type_without_nullable(column.full_type or column.db_type)
        server_declared_type = ClickhouseTypeNames.get_server_type(declared_type)
        return "".join(observed_type.split()) != "".join(server_declared_type.split())

    @classmethod
    def reports_nullability(cls, column: ColumnInfo) -> bool:
        # A container is never Nullable - its NULL is written as an empty one.
        return not column.db_type.startswith(CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES)

    @classmethod
    def map_column_type(cls, column: ColumnInfo) -> tuple[str, dict[str, Any], bool]:
        """Maps a column by the name of its type without arguments - matched in full, as one name
        holds another (``UInt32``, ``Array(Int64)``).

        Args:
            column: The introspected column.

        Returns:
            (field_path, extra_kwargs, is_ambiguous) - a type with no field is the most general
            TextField, marked ambiguous.
        """
        # The type as reported, its LowCardinality(...) kept - the column's NULLs are its own flag.
        type_sql, _nullable = ClickhouseTypeParser.unwrap(column.full_type or column.db_type)
        specification = ClickhouseTypeParser.get_specification(type_sql)
        kwargs = {name: value for name, value in specification.kwargs.items() if name != "null"}
        is_ambiguous = (
            not ClickhouseTypeParser.holds_only_mapped_types(specification)
            or specification.path == CLICKHOUSE_ENUM_LABEL_FIELD_PATH
        )
        return specification.path, kwargs, is_ambiguous

    @staticmethod
    def unwrap_type(type_sql: str) -> tuple[str, bool]:
        """A column type without the wrappers that change no field, and whether it holds NULLs.

        Args:
            type_sql: The type as ``system.columns`` reports it.

        Returns:
            The type, and whether the column is nullable.
        """
        nullable = False
        unwrapped = True
        while unwrapped:
            unwrapped = False
            for wrapper in CLICKHOUSE_TYPE_WRAPPERS:
                if type_sql.startswith(wrapper) and type_sql.endswith(")"):
                    nullable = nullable or wrapper == CLICKHOUSE_TYPE_WRAPPERS[0]
                    type_sql = type_sql[len(wrapper) : -1]
                    unwrapped = True
        return type_sql, nullable

    @staticmethod
    def get_decimal_precision_scale(db_type: str) -> tuple[int | None, int | None]:
        """The precision and scale of a ``Decimal(P, S)`` type.

        Args:
            db_type: The unwrapped type.

        Returns:
            The precision and scale, None for any other type.
        """
        if not db_type.startswith("Decimal(") or not db_type.endswith(")"):
            return None, None
        precision_sql, _, scale_sql = db_type[len("Decimal(") : -1].partition(",")
        return int(precision_sql), int(scale_sql or 0)
