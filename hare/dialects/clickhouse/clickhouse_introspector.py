from __future__ import annotations

import datetime
import struct
import uuid
from collections.abc import Collection, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_CURRENT_DATABASE_SQL,
    CLICKHOUSE_DATABASE_EXISTS_SQL,
    CLICKHOUSE_DICTIONARY_NAMES_SQL,
    CLICKHOUSE_INDEX_TYPE_ARGUMENT_DEFAULTS,
    CLICKHOUSE_INDEX_TYPE_ARGUMENTS,
    CLICKHOUSE_TABLE_EXISTS_SQL,
    CLICKHOUSE_TABLE_NAMES_SQL,
    CLICKHOUSE_TYPE_MAP,
    CLICKHOUSE_VIEWS_SQL,
)
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.indexes import (
    BloomFilterIndex,
    MinMaxIndex,
    NgramBloomFilterIndex,
    SetIndex,
    TokenBloomFilterIndex,
)
from hare.dialects.clickhouse.introspection.clickhouse_catalog_parts import ClickhouseCatalogParts
from hare.dialects.clickhouse.introspection.clickhouse_catalog_reader import ClickhouseCatalogReader
from hare.dialects.clickhouse.introspection.clickhouse_declaration_mapper import ClickhouseDeclarationMapper
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.clickhouse_type_parser import ClickhouseTypeParser
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_CAST_DEFAULT_PATTERN,
    CLICKHOUSE_DATE_DEFAULT_PATTERN,
    CLICKHOUSE_DECIMAL_DEFAULT_PATTERN,
    CLICKHOUSE_FLOAT_DEFAULT_PATTERN,
    CLICKHOUSE_MOMENT_DEFAULT_PATTERN,
    CLICKHOUSE_QUOTED_STRING_PATTERN,
    CLICKHOUSE_TEXT_VALUE_DEFAULT_PATTERN,
    CLICKHOUSE_WIDE_INTEGER_DEFAULT_PATTERN,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_ENUM_LABEL_FIELD_PATH,
    CLICKHOUSE_INSPECTED_ENUM_NAME,
    CLICKHOUSE_INT_ENUM_FIELD_PATH,
)
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.inspectdb.enums import SchemaPartType
from hare.inspectdb.exceptions import SchemaNotFoundError
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect


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
            The text of each argument by its name - ClickHouse's default of one left out - with the granularity.
        """
        _, arguments = ClickhouseTypeNames.get_type_parts(type_sql)
        argument_names = CLICKHOUSE_INDEX_TYPE_ARGUMENTS.get(index_type, ())
        return {
            "granularity": str(granularity),
            **CLICKHOUSE_INDEX_TYPE_ARGUMENT_DEFAULTS.get(index_type, {}),
            **dict(zip(argument_names, arguments, strict=False)),
        }

    @classmethod
    def parse_db_default(cls, raw_sql: str) -> Any:
        """Reads the typed literals hare writes a default as - a float by its bytes, a decimal of its
        own scale, a wide integer, a date, a moment, a UUID or an address from its text, a cast to a
        ``Dynamic`` or ``Variant`` type - back as their values; anything else as
        ``SchemaIntrospector.parse_db_default()`` does.

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
        wide_integer_match = CLICKHOUSE_WIDE_INTEGER_DEFAULT_PATTERN.fullmatch(sql)
        if wide_integer_match is not None:
            return int(wide_integer_match.group(1))
        date_match = CLICKHOUSE_DATE_DEFAULT_PATTERN.fullmatch(sql)
        if date_match is not None:
            return datetime.date.fromisoformat(date_match.group(1))
        moment_match = CLICKHOUSE_MOMENT_DEFAULT_PATTERN.fullmatch(sql)
        if moment_match is not None:
            return datetime.datetime.fromisoformat(moment_match.group(1)).replace(tzinfo=datetime.UTC)
        text_value_match = CLICKHOUSE_TEXT_VALUE_DEFAULT_PATTERN.fullmatch(sql)
        if text_value_match is not None:
            text = text_value_match.group(2)
            return uuid.UUID(text) if text_value_match.group(1) == ClickhouseTypeName.UUID else text
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
        if (
            type_name not in {ClickhouseTypeName.ENUM8, ClickhouseTypeName.ENUM16}
            or isinstance(value, bool)
            or not isinstance(value, (int, str))
        ):
            return value
        numbers_by_label = ClickhousePhysicalType.get_enum_members(arguments)
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
        return ClickhouseSqlParts.get_string_text(literal_sql)

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
        snapshot = await ClickhouseCatalogReader.read_objects(connection, schema, object_names)
        return ClickhouseCatalogParts.get_objects(snapshot)

    @classmethod
    def get_table_infos(cls, snapshot: CatalogSnapshot, dialect: Dialect) -> list[TableInfo]:
        return ClickhouseDeclarationMapper(cls).get_tables(snapshot)

    @classmethod
    def get_table_parts(cls, snapshot: CatalogSnapshot, table_infos: list[TableInfo]) -> list[SchemaPart]:
        return ClickhouseCatalogParts.get(snapshot, table_infos)

    @classmethod
    async def fetch_catalog(cls, connection: DatabaseClient, tables: list[str], schema: str) -> CatalogSnapshot:
        """Capture raw catalog metadata without reconstructing model declarations."""
        return await ClickhouseCatalogReader.read_tables(connection, tables, schema)

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
        physical_type = ClickhousePhysicalType.from_sql(type_sql)
        return physical_type.unwrapped.sql, physical_type.nullable

    @staticmethod
    def get_decimal_precision_scale(db_type: str) -> tuple[int | None, int | None]:
        """The precision and scale of a ``Decimal(P, S)`` type.

        Args:
            db_type: The unwrapped type.

        Returns:
            The precision and scale, None for any other type.
        """
        physical_type = ClickhousePhysicalType.from_sql(db_type)
        if physical_type.name != ClickhouseTypeName.DECIMAL:
            return None, None
        return physical_type.precision, physical_type.scale
