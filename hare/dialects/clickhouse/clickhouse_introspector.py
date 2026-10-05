from __future__ import annotations

import re
import struct
import uuid
from collections.abc import Collection, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_COLUMNS_SQL,
    CLICKHOUSE_CURRENT_DATABASE_SQL,
    CLICKHOUSE_DATABASE_EXISTS_SQL,
    CLICKHOUSE_DEFAULT_INDEX_TYPE,
    CLICKHOUSE_DICTIONARY_NAMES_SQL,
    CLICKHOUSE_EPHEMERAL_COLUMN_TODO,
    CLICKHOUSE_EPHEMERAL_COLUMN_TYPE,
    CLICKHOUSE_GENERATED_COLUMN_TYPES,
    CLICKHOUSE_INDEX_TYPE_ARGUMENTS,
    CLICKHOUSE_INDEXES_SQL,
    CLICKHOUSE_OBJECT_DEFINITIONS_SQL,
    CLICKHOUSE_OBJECT_PART_TYPES,
    CLICKHOUSE_TABLE_EXISTS_SQL,
    CLICKHOUSE_TABLE_NAMES_SQL,
    CLICKHOUSE_TABLE_SQL,
    CLICKHOUSE_TYPE_MAP,
    CLICKHOUSE_TYPE_WRAPPERS,
    CLICKHOUSE_VIEWS_SQL,
    CLICKHOUSE_VIRTUAL_COLUMN_TYPE,
)
from hare.dialects.clickhouse.drivers.clickhouse_shared_variant_values import ClickhouseSharedVariantValues
from hare.dialects.clickhouse.indexes import (
    BloomFilterIndex,
    MinMaxIndex,
    NgramBloomFilterIndex,
    SetIndex,
    TokenBloomFilterIndex,
)
from hare.dialects.clickhouse.introspection.clickhouse_check_constraints import ClickhouseCheckConstraints
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.clickhouse_type_parser import ClickhouseTypeParser
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_CAST_DEFAULT_PATTERN,
    CLICKHOUSE_DECIMAL_DEFAULT_PATTERN,
    CLICKHOUSE_ESCAPED_CHARACTERS,
    CLICKHOUSE_FLOAT_DEFAULT_PATTERN,
    CLICKHOUSE_QUOTED_STRING_PATTERN,
    CLICKHOUSE_STRING_ESCAPE_PATTERN,
    CLICKHOUSE_TEXT_VALUE_DEFAULT_PATTERN,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_ENUM_LABEL_FIELD_PATH,
    CLICKHOUSE_INSPECTED_ENUM_NAME,
    CLICKHOUSE_INT_ENUM_FIELD_PATH,
)
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.inspectdb.exceptions import SchemaNotFoundError
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo

if TYPE_CHECKING:
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
    def parse_db_default(cls, raw_sql: str) -> Any:
        """Reads the typed literals hare writes a default as - a float by its bytes, a decimal of its
        own scale, a UUID or an address from its text, a cast to a ``Dynamic`` or ``Variant`` type - back
        as their values; anything else as ``SchemaIntrospector.parse_db_default()`` does.

        Args:
            raw_sql: The default's expression as the server keeps it.

        Returns:
            The default.
        """
        sql = raw_sql.strip()
        cast_match = CLICKHOUSE_CAST_DEFAULT_PATTERN.fullmatch(sql)
        if cast_match is not None:
            return cls.parse_db_default(cast_match.group(1))
        float_match = CLICKHOUSE_FLOAT_DEFAULT_PATTERN.fullmatch(sql)
        if float_match is not None:
            float_format = "<d" if float_match.group(1) == "64" else "<f"
            return struct.unpack(float_format, bytes.fromhex(float_match.group(2)))[0]
        decimal_match = CLICKHOUSE_DECIMAL_DEFAULT_PATTERN.fullmatch(sql)
        if decimal_match is not None:
            return Decimal(decimal_match.group(1))
        text_value_match = CLICKHOUSE_TEXT_VALUE_DEFAULT_PATTERN.fullmatch(sql)
        if text_value_match is not None:
            text = text_value_match.group(2)
            return uuid.UUID(text) if text_value_match.group(1) == "UUID" else text
        return super().parse_db_default(sql)

    @classmethod
    def get_column_default(cls, expression: str, type_sql: str) -> Any:
        """A column's default as its field holds it - an enum's member by its label for a
        ``CharEnumField``, by its number for an ``IntEnumField``, whichever the server keeps.

        Args:
            expression: The default's expression as the server keeps it.
            type_sql: The column's type, its wrappers left out.

        Returns:
            The default.
        """
        value = cls.parse_db_default(expression)
        type_name, arguments = ClickhouseTypeNames.get_type_parts(type_sql)
        if type_name not in {"Enum8", "Enum16"} or isinstance(value, bool) or not isinstance(value, int | str):
            return value
        numbers_by_label = ClickhouseSharedVariantValues.get_enum_numbers(arguments)
        specification = ClickhouseTypeParser.get_enum_specification(
            type_name, arguments, CLICKHOUSE_INSPECTED_ENUM_NAME
        )
        if specification.path == CLICKHOUSE_INT_ENUM_FIELD_PATH:
            return numbers_by_label.get(value, value) if isinstance(value, str) else value
        labels_by_number = {number: label for label, number in numbers_by_label.items()}
        return labels_by_number.get(value, value) if isinstance(value, int) else value

    @classmethod
    def parse_string_literal(cls, literal_sql: str) -> str | None:
        # The server writes a quote, a backslash and a control character of a string escaped by a
        # backslash.
        quoted_match = CLICKHOUSE_QUOTED_STRING_PATTERN.fullmatch(literal_sql)
        if quoted_match is None:
            return None
        return CLICKHOUSE_STRING_ESCAPE_PATTERN.sub(cls.get_escaped_character, quoted_match.group(1))

    @staticmethod
    def get_escaped_character(escape_match: re.Match[str]) -> str:
        """The character an escape of a string literal stands for.

        Args:
            escape_match: The escape, without its backslash in its group.

        Returns:
            The character.
        """
        escaped = escape_match.group(1)
        if len(escaped) == 3:
            return chr(int(escaped[1:], 16))
        return CLICKHOUSE_ESCAPED_CHARACTERS.get(escaped, escaped)

    @classmethod
    async def fetch_default_schema(cls, connection: DatabaseClient) -> str:
        rows = await connection.execute_dicts(CLICKHOUSE_CURRENT_DATABASE_SQL)
        return str(rows[0]["name"])

    @classmethod
    async def fetch_table_names(cls, connection: DatabaseClient, schema: str, include_partitions: bool) -> list[str]:
        rows = await connection.execute_dicts(CLICKHOUSE_TABLE_NAMES_SQL, [schema])
        if not rows and not await connection.execute_dicts(CLICKHOUSE_DATABASE_EXISTS_SQL, [schema]):
            raise SchemaNotFoundError(schema)
        return [row["name"] for row in rows]

    @classmethod
    async def fetch_table_exists(cls, connection: DatabaseClient, table: str) -> bool:
        return bool(await connection.execute_dicts(CLICKHOUSE_TABLE_EXISTS_SQL, [table]))

    @classmethod
    async def fetch_views(cls, connection: DatabaseClient, schema: str) -> list[tuple[str, bool, tuple[str, ...]]]:
        rows = await connection.execute_dicts(CLICKHOUSE_VIEWS_SQL, [schema])
        return [(row["name"], bool(row["is_materialized"]), ()) for row in rows]

    @classmethod
    async def fetch_schema_object_notes(cls, connection: DatabaseClient, schema: str) -> list[str]:
        rows = await connection.execute_dicts(CLICKHOUSE_DICTIONARY_NAMES_SQL, [schema])
        return [
            f"# NOTE: dictionary {row['name']!r} isn't a model - declare it in Meta.dictionaries of the model it "
            f"reads (ClickhouseDictionary({row['name']!r}, ...))."
            for row in rows
        ]

    @classmethod
    async def fetch_object_parts(
        cls, connection: DatabaseClient, schema: str, object_names: Mapping[SchemaPartType, Collection[str]]
    ) -> list[SchemaPart]:
        if not any(object_names.get(part_type) for part_type in CLICKHOUSE_OBJECT_PART_TYPES.values()):
            return []
        parts: list[SchemaPart] = []
        for row in await connection.execute_dicts(CLICKHOUSE_OBJECT_DEFINITIONS_SQL, [schema]):
            part_type = CLICKHOUSE_OBJECT_PART_TYPES[str(row["engine"])]
            if row["name"] in object_names.get(part_type, ()):
                parts.append(SchemaPart(part_type, "", row["name"], {"query": row["create_table_query"]}))
        return parts

    @classmethod
    async def fetch_tables(cls, connection: DatabaseClient, tables: list[str], schema: str) -> list[TableInfo]:
        default_schema = await cls.fetch_default_schema(connection)
        return [await cls.fetch_table(connection, table, schema, schema == default_schema) for table in tables]

    @classmethod
    async def fetch_table(
        cls, connection: DatabaseClient, table: str, schema: str, is_in_default_schema: bool
    ) -> TableInfo:
        """Reads one table: its columns, primary key, data skipping indexes, storage options and
        comment.

        Args:
            connection: The connection.
            table: The table.
            schema: The database it lives in.
            is_in_default_schema: Whether that is the connection's database.

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
            for key_sql in ClickhouseSqlParts.split(
                ClickhouseSqlParts.get_unwrapped(str(table_rows[0]["primary_key"])) if table_rows else ""
            )
            if (identifier := ClickhouseSqlParts.get_identifier(ClickhouseSqlParts.get_unwrapped(key_sql)))
            in column_names
        ]
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        for row in await connection.execute_dicts(CLICKHOUSE_INDEXES_SQL, [schema, table]):
            expression = ClickhouseSqlParts.get_unwrapped(str(row["expr"]))
            # An index over columns lists them, comma-separated - a name quoted when it isn't plain
            # ASCII; one with a key of another expression is rebuilt from its keys' SQL, each kept whole.
            parts = [part.strip() for part in ClickhouseSqlParts.split(expression)]
            part_identifiers = [ClickhouseSqlParts.get_identifier(part) for part in parts]
            reads_columns_only = all(identifier in column_names for identifier in part_identifiers)
            index_columns = cast("list[str]", part_identifiers) if reads_columns_only else []
            # A minmax index of granularity 1 is the one hare writes for an index of no type. Over one
            # column it is a field flag, as a plain single-column index is on the other databases.
            index_type = str(row["type"])
            granularity = int(row["granularity"])
            is_default_type = index_type == CLICKHOUSE_DEFAULT_INDEX_TYPE and granularity == 1
            if is_default_type and len(index_columns) == 1:
                column_indexes.append(IndexInfo(columns=index_columns, is_unique=False, name=str(row["name"])))
            else:
                indexes.append(
                    IndexInfo(
                        columns=index_columns,
                        is_unique=False,
                        name=str(row["name"]),
                        expression_terms=None if reads_columns_only else parts,
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
                    db_default=cls.get_column_default(str(row["default_expression"]), db_type)
                    if row["default_type"] == "DEFAULT"
                    else None,
                    generated_expression=str(row["default_expression"])
                    if row["default_type"] in CLICKHOUSE_GENERATED_COLUMN_TYPES
                    else None,
                    generated_stored=row["default_type"] != CLICKHOUSE_VIRTUAL_COLUMN_TYPE,
                    description=str(row["comment"]) or None,
                    full_type=str(row["type"]),
                    has_index=name in indexed_columns,
                    todo_reason=CLICKHOUSE_EPHEMERAL_COLUMN_TODO
                    if row["default_type"] == CLICKHOUSE_EPHEMERAL_COLUMN_TYPE
                    else None,
                )
            )
        return TableInfo(
            name=table,
            schema=schema,
            is_in_default_schema=is_in_default_schema,
            columns=columns,
            indexes=indexes,
            column_indexes=column_indexes,
            table_description=(str(table_rows[0]["comment"]) or None) if table_rows else None,
            check_constraints=ClickhouseCheckConstraints.get_check_constraints(
                str(table_rows[0]["create_table_query"])
            )
            if table_rows
            else [],
            table_options=await ClickhouseObservedTableOptions.fetch(
                connection, table_rows[0], column_rows, primary_key_columns
            )
            if table_rows
            else None,
        )

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
        specification = ClickhouseTypeParser.get_specification(
            type_sql, f"{column.name}_{CLICKHOUSE_INSPECTED_ENUM_NAME}"
        )
        kwargs = {name: value for name, value in specification.kwargs.items() if name != "null"}
        if "enum_type" in kwargs:
            # A column without a comment keeps none - the field would describe its members otherwise.
            kwargs["description"] = column.description or ""
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
