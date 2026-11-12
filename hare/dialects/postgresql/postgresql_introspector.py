from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.postgresql.constants import (
    NATIVE_ENUM_FIELD_PATH,
    POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP,
    POSTGRESQL_ARRAY_FIELD_PATH,
    POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES,
    POSTGRESQL_DEFAULT_SCHEMA,
    POSTGRESQL_ENUM_TYPES_SQL,
    POSTGRESQL_EXACT_TYPE_FIELD_PATHS,
    POSTGRESQL_EXTENSION_TYPE_FIELD_PATHS,
    POSTGRESQL_EXTENSIONS_SQL,
    POSTGRESQL_FIXED_CHAR_FIELD_PATH,
    POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_FUNCTIONS_SQL,
    POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL,
    POSTGRESQL_NAIVE_TYPE_FIELD_PATHS,
    POSTGRESQL_NUMERIC_CAST_TYPES,
    POSTGRESQL_SCHEMA_EXISTS_SQL,
    POSTGRESQL_SCHEMA_NAMES_SQL,
    POSTGRESQL_TABLE_EXISTS_SQL,
    POSTGRESQL_TABLE_NAMES_SQL,
    POSTGRESQL_TRAILING_TYPE_CAST_RE,
    POSTGRESQL_TS_VECTOR_FIELD_PATH,
    POSTGRESQL_TS_VECTOR_TYPE_NAME,
    POSTGRESQL_TUNED_INDEX_TYPES,
    POSTGRESQL_TYPE_MAP,
    POSTGRESQL_UNATTACHED_SEQUENCES_SQL,
    POSTGRESQL_VIEWS_SQL,
)
from hare.dialects.postgresql.fields.constants import NATIVE_ENUM_TYPE_NAME_PATTERN
from hare.dialects.postgresql.indexes.declarations import (
    BloomIndex,
    BrinIndex,
    GinIndex,
    GistIndex,
    HashIndex,
    SpGistIndex,
)
from hare.dialects.postgresql.indexes.hnsw_index import HnswIndex
from hare.dialects.postgresql.indexes.ivfflat_index import IvfflatIndex
from hare.dialects.postgresql.introspection.postgresql_array_literals import PostgresqlArrayLiterals
from hare.dialects.postgresql.introspection.postgresql_catalog_parts import PostgresqlCatalogParts
from hare.dialects.postgresql.introspection.postgresql_catalog_reader import PostgresqlCatalogReader
from hare.dialects.postgresql.introspection.postgresql_declaration_mapper import PostgresqlDeclarationMapper
from hare.dialects.postgresql.introspection.postgresql_inspected_schema_objects import PostgresqlInspectedSchemaObjects
from hare.dialects.postgresql.introspection.postgresql_object_grants import PostgresqlObjectGrants
from hare.dialects.postgresql.introspection.postgresql_object_parts import PostgresqlObjectParts
from hare.dialects.postgresql.search.ts_vector_expression_reader import TSVectorExpressionReader
from hare.dialects.postgresql.spatial.constants import (
    POSTGIS_COLUMN_TYPE_PATTERN,
    POSTGIS_GEOGRAPHY_TYPE,
    POSTGIS_GEOMETRY_TYPE,
    POSTGIS_GEOMETRY_TYPE_NAMES,
)
from hare.dialects.postgresql.types.postgresql_physical_type import PostgresqlPhysicalType
from hare.gis.constants import DEFAULT_SRID, GEOMETRY_FIELD_CLASS_NAMES
from hare.inspectdb.constants import (
    AMBIGUOUS_REASON_SENTINEL_KWARG,
    BASE_FIELD_SENTINEL_KWARG,
    INSPECTED_ENUM_MODULE_NAME,
)
from hare.inspectdb.enums import SchemaPartType
from hare.inspectdb.exceptions import SchemaNotFoundError
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.inspected_field_specification import InspectedFieldSpecification
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo

if TYPE_CHECKING:
    from hare.ddl.schema_objects.database_function import DatabaseFunction
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect


class PostgresqlIntrospector(SchemaIntrospector):
    """Reads a PostgreSQL schema from its catalog (``pg_catalog`` and ``information_schema``) -
    every table of a request with one query per type of metadata, whatever the number of tables."""

    TYPE_MAP = POSTGRESQL_TYPE_MAP
    # Now() is the moment of the statement - now() and CURRENT_TIMESTAMP, the start of the
    # transaction, stay SqlDefault.
    NOW_EXPRESSIONS = ("statement_timestamp()",)
    SEQUENCE_DEFAULT_PREFIXES = ("nextval(",)
    # PostgreSQL echoes a negative or out-of-int4-range numeric default as a quoted, cast literal
    # ("'-1.5'::double precision", "'9000000000'::bigint").
    NUMERIC_CAST_TYPES = POSTGRESQL_NUMERIC_CAST_TYPES
    INDEX_CLASSES_BY_TYPE = {
        "gin": GinIndex,
        "gist": GistIndex,
        "hash": HashIndex,
        "brin": BrinIndex,
        "spgist": SpGistIndex,
        "bloom": BloomIndex,
        "hnsw": HnswIndex,
        "ivfflat": IvfflatIndex,
    }
    TUNED_INDEX_TYPES = POSTGRESQL_TUNED_INDEX_TYPES

    @classmethod
    def split_type_cast(cls, sql: str) -> tuple[str, str | None]:
        """The ``::type`` ending an expression PostgreSQL echoes back, split off."""
        cast_match = POSTGRESQL_TRAILING_TYPE_CAST_RE.match(sql)
        if cast_match is None:
            return sql, None
        return cast_match.group("sql"), cast_match.group("cast_type")

    @classmethod
    def parse_array_literal(cls, text: str, element_type: str) -> list[Any] | None:
        """An array literal - ``{}``, ``{1,2}``, ``{"a b",NULL}``, ``{{1,2},{3,4}}`` - as a list, its
        elements read by ``element_type``: an integer, a number, a boolean, else text.

        Args:
            text: The literal's text.
            element_type: The type of its elements.

        Returns:
            The list, None for text that isn't such a literal.
        """
        try:
            array, end = PostgresqlArrayLiterals.read_array_literal(text.strip(), 0, element_type)
        except ValueError:
            return None
        return array if end == len(text.strip()) else None

    @classmethod
    def is_unnamed_index_name(cls, name: str, table_name: str, column_names: Sequence[str]) -> bool:
        """An unnamed index is ``<table>_<columns>_idx``, an unnamed UNIQUE constraint
        ``<table>_<columns>_key``."""
        name_stem = "_".join((table_name, *column_names))
        return super().is_unnamed_index_name(name, table_name, column_names) or any(
            name == f"{name_stem}_{suffix}" for suffix in POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES
        )

    @classmethod
    async def fetch_default_schema(cls, connection: DatabaseClient) -> str:
        # current_schema() - the first existing schema on the search_path.
        rows = await connection.execute_dicts("SELECT current_schema() AS schema_name")
        return rows[0]["schema_name"] or POSTGRESQL_DEFAULT_SCHEMA

    @classmethod
    async def fetch_schema_names(cls, connection: DatabaseClient) -> list[str]:
        rows = await connection.execute_dicts(POSTGRESQL_SCHEMA_NAMES_SQL)
        return [row["schema_name"] for row in rows]

    @classmethod
    async def fetch_unattached_schema_objects(
        cls, connection: DatabaseClient, schema: str
    ) -> tuple[list[DatabaseSequence], list[DatabaseFunction], list[SchemaPart]]:
        sequence_rows = await connection.execute_dicts(POSTGRESQL_UNATTACHED_SEQUENCES_SQL, [schema])
        function_rows = await connection.execute_dicts(POSTGRESQL_INSPECTED_FUNCTIONS_SQL, [schema])
        sequence_privilege_rows = await connection.execute_dicts(
            POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL, [schema]
        )
        function_privilege_rows = await connection.execute_dicts(
            POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL, [schema]
        )
        function_signatures: dict[str, list[str]] = defaultdict(list)
        for row in function_rows:
            function_signatures[row["name"]].append(row["identity_arguments"])
        return (
            [PostgresqlInspectedSchemaObjects.get_sequence(row, None) for row in sequence_rows],
            [PostgresqlInspectedSchemaObjects.get_function(row) for row in function_rows],
            [
                *PostgresqlObjectGrants.get_parts(sequence_privilege_rows, GrantTarget.SEQUENCE),
                *PostgresqlObjectGrants.get_parts(
                    function_privilege_rows,
                    GrantTarget.FUNCTION,
                    function_signatures={name: tuple(signatures) for name, signatures in function_signatures.items()},
                ),
            ],
        )

    @classmethod
    async def fetch_extensions(cls, connection: DatabaseClient) -> set[str]:
        return {row["name"] for row in await connection.execute_dicts(POSTGRESQL_EXTENSIONS_SQL)}

    @classmethod
    async def fetch_enum_types(
        cls, connection: DatabaseClient, default_schema: str
    ) -> dict[tuple[str | None, str], EnumType]:
        enum_types: dict[tuple[str | None, str], EnumType] = {}
        for row in await connection.execute_dicts(POSTGRESQL_ENUM_TYPES_SQL):
            labels = tuple(row["labels"])
            schema = row["schema_name"]
            enum_types[schema, row["name"]] = EnumType(name=row["name"], labels=labels, schema=schema)
            if schema == default_schema:
                # A model without Meta.schema names it without one.
                enum_types[None, row["name"]] = EnumType(name=row["name"], labels=labels)
        return enum_types

    @classmethod
    async def fetch_views(cls, connection: DatabaseClient, schema: str) -> list[tuple[str, bool, tuple[str, ...]]]:
        rows = await connection.execute_dicts(POSTGRESQL_VIEWS_SQL, [schema])
        return [(row["name"], row["is_materialized"], tuple(row["trigger_names"])) for row in rows]

    @classmethod
    async def fetch_table_names(cls, connection: DatabaseClient, schema: str, include_partitions: bool) -> list[str]:
        rows = await connection.execute_dicts(POSTGRESQL_TABLE_NAMES_SQL, [schema, include_partitions])
        if not rows and not await connection.execute_dicts(POSTGRESQL_SCHEMA_EXISTS_SQL, [schema]):
            raise SchemaNotFoundError(schema)
        return [row["tablename"] for row in rows]

    @classmethod
    async def fetch_table_exists(cls, connection: DatabaseClient, table: str) -> bool:
        _, rows = await connection.execute(POSTGRESQL_TABLE_EXISTS_SQL, [table])
        return bool(rows)

    @classmethod
    async def fetch_object_parts(
        cls, connection: DatabaseClient, schema: str, object_names: Mapping[SchemaPartType, Collection[str]]
    ) -> list[SchemaPart]:
        snapshot = await PostgresqlCatalogReader.read_objects(connection, schema, object_names)
        return PostgresqlObjectParts.get(snapshot)

    @classmethod
    def get_table_infos(cls, snapshot: CatalogSnapshot, dialect: Dialect) -> list[TableInfo]:
        return PostgresqlDeclarationMapper(cls).get_tables(snapshot)

    @classmethod
    def maps_unbounded_char_to_text(cls) -> bool:
        return True

    @staticmethod
    def get_canonical_type(type_sql: str) -> str | None:
        """The name ``format_type()`` reports a type under.

        Args:
            type_sql: A type as declared (``VARCHAR(50)``, ``TIMESTAMPTZ``) or reported.

        Returns:
            ``character varying(50)``, ``timestamp with time zone``; None for a type outside
            POSTGRESQL_CANONICAL_TYPE_NAMES.
        """
        return PostgresqlPhysicalType.from_sql(type_sql).canonical_sql

    @classmethod
    def column_types_differ(cls, declared_type: str, column: ColumnInfo) -> bool:
        """Whether both types normalize to different ``format_type()`` names, or a column of a type of
        its own (an enum, a domain, an extension's) now holds a built-in one - another spelling
        outside the known types is never reported.

        Args:
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            Whether they differ.
        """
        observed_type = cls.get_canonical_type(column.full_type or "")
        expected_type = cls.get_canonical_type(declared_type)
        if expected_type is None:
            return observed_type is not None
        return observed_type is not None and observed_type != expected_type

    @classmethod
    def get_generated_field(
        cls, column: ColumnInfo, attribute_names_by_column: dict[str, str]
    ) -> tuple[str, dict[str, Any]] | None:
        if column.udt_name != POSTGRESQL_TS_VECTOR_TYPE_NAME or not column.generated_stored:
            return None
        kwargs = TSVectorExpressionReader.get_field_kwargs(column.generated_expression, attribute_names_by_column)  # type: ignore[arg-type]
        return None if kwargs is None else (POSTGRESQL_TS_VECTOR_FIELD_PATH, kwargs)

    @staticmethod
    def get_native_enum_kwargs(column: ColumnInfo) -> dict[str, Any] | None:
        """The arguments of the ``NativeEnumField`` of a column of an ``ENUM`` type - an enum class
        of its labels, named after the type, the type's name, and its schema when it isn't the default one.

        Args:
            column: The introspected column.

        Returns:
            The arguments; None for a column of no enum type, or of one no NativeEnumField declares
            (a name that isn't a lowercase identifier, no label).
        """
        type_name = (column.udt_name or "").removeprefix("_")
        labels = column.enum_labels
        if not labels or not NATIVE_ENUM_TYPE_NAME_PATTERN.fullmatch(type_name):
            return None
        enum_type = StrEnum(  # type: ignore[misc]
            ModelNaming.get_class_name(type_name),
            ModelNaming.get_enum_member_names(labels),
            module=INSPECTED_ENUM_MODULE_NAME,
        )
        kwargs: dict[str, Any] = {"enum_type": enum_type, "type_name": type_name}
        # A type outside the connection's default schema is named with its schema.
        if column.enum_type_schema is not None:
            kwargs["type_schema"] = column.enum_type_schema
        # A column without a comment keeps none - the field would describe its members otherwise.
        kwargs["description"] = column.description or ""
        return kwargs

    @classmethod
    def map_column_type(cls, column: ColumnInfo) -> tuple[str, dict[str, Any], bool]:
        """Maps a column onto a field - an ARRAY by its element type, the extension types hare has
        fields for (citext, hstore, PostGIS geography, pgvector) onto those. Fixed-width ``character``
        uses ``FixedCharField``; timestamp/time without time zone and interval use
        ``NaiveDatetimeField``, ``NaiveTimeField`` and ``IntervalField`` respectively. Unsupported
        user-defined or array element types remain ambiguous text; other types use ``TYPE_MAP``.

        Args:
            column: The introspected column.

        Returns:
            (field_path, extra_kwargs, is_ambiguous).
        """
        lowered = column.db_type.lower()
        native_enum_kwargs = cls.get_native_enum_kwargs(column)
        if column.db_type == "ARRAY":
            element_type_name = (column.udt_name or "").removeprefix("_")
            element_mapping = (
                (NATIVE_ENUM_FIELD_PATH, native_enum_kwargs)
                if native_enum_kwargs is not None
                else POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP.get(element_type_name)
            )
            if element_mapping is None:
                return "hare.fields.data.text.TextField", {}, True
            element_path, element_kwargs = element_mapping
            element_kwargs = dict(element_kwargs)
            # information_schema reports length/precision/scale as NULL for an ARRAY column - the
            # real element size is recovered from format_type()'s own rendering instead.
            if (
                element_path in {"hare.fields.data.text.CharField", POSTGRESQL_FIXED_CHAR_FIELD_PATH}
                and column.array_element_max_length is not None
            ):
                element_kwargs["max_length"] = column.array_element_max_length
            if element_path == "hare.fields.data.numeric.DecimalField" and (
                precision_scale := column.array_element_numeric_precision_scale
            ):
                element_kwargs["max_digits"], element_kwargs["decimal_places"] = precision_scale
            # An array of arrays per further dimension the column was declared with.
            for _dimension in range(column.array_dimensions - 1):
                element_path, element_kwargs = (
                    POSTGRESQL_ARRAY_FIELD_PATH,
                    {"base_field": InspectedFieldSpecification(element_path, element_kwargs)},
                )
            return (
                POSTGRESQL_ARRAY_FIELD_PATH,
                {BASE_FIELD_SENTINEL_KWARG: (element_path, element_kwargs)},
                False,
            )
        if column.db_type == "USER-DEFINED":
            if native_enum_kwargs is not None:
                return NATIVE_ENUM_FIELD_PATH, native_enum_kwargs, False
            if column.udt_name in {POSTGIS_GEOMETRY_TYPE, POSTGIS_GEOGRAPHY_TYPE}:
                spatial_mapping = cls.get_spatial_field_mapping(column.full_type or "")
                if spatial_mapping is not None:
                    return spatial_mapping
                if column.full_type == POSTGIS_GEOGRAPHY_TYPE:
                    return "hare.gis.fields.GeometryField", {"geography": True}, False
            if column.udt_name in POSTGRESQL_EXTENSION_TYPE_FIELD_PATHS:
                # An extension type, but a real, unambiguous hare-orm field - unlike an arbitrary
                # enum/composite type.
                return POSTGRESQL_EXTENSION_TYPE_FIELD_PATHS[column.udt_name], {}, False
            if column.udt_name == "vector" and column.vector_dimensions is not None:
                # VectorField's dimensions= is required - without a parsed dimension count this
                # falls through to the generic user-defined type below.
                return (
                    "hare.vectors.vector_field.VectorField",
                    {"dimensions": column.vector_dimensions},
                    False,
                )
            # Another user-defined type, or an enum no NativeEnumField declares.
            udt_name = column.udt_name or "?"
            return (
                "hare.fields.data.text.TextField",
                {AMBIGUOUS_REASON_SENTINEL_KWARG: f"user-defined type {udt_name!r}"},
                True,
            )
        if lowered in POSTGRESQL_EXACT_TYPE_FIELD_PATHS:
            return POSTGRESQL_EXACT_TYPE_FIELD_PATHS[lowered], {}, False
        if lowered == "character":
            # Fixed-width and space-padded - CharField writes a varchar.
            return POSTGRESQL_FIXED_CHAR_FIELD_PATH, {"max_length": column.max_length or 1}, False
        if lowered in POSTGRESQL_NAIVE_TYPE_FIELD_PATHS:
            # A wall clock without a time zone - DatetimeField and TimeField write timestamptz and
            # timetz; an interval - TimeDeltaField writes a bigint of microseconds.
            return POSTGRESQL_NAIVE_TYPE_FIELD_PATHS[lowered], {}, False
        return super().map_column_type(column)

    @staticmethod
    def get_spatial_field_mapping(full_type: str) -> tuple[str, dict[str, Any], bool] | None:
        """The GIS field of a spatial column, preserving geometry type, geography, SRID and dimensions.

        Args:
            full_type: The column's ``format_type()``.

        Returns:
            (field_path, extra_kwargs, is_ambiguous), None for a column without a type and an SRID or
            with measured (m) coordinates - read as a user-defined type.
        """
        match = POSTGIS_COLUMN_TYPE_PATTERN.fullmatch(full_type)
        if match is None or match.group("measured"):
            return None
        is_geography = match.group("column_type").lower() == POSTGIS_GEOGRAPHY_TYPE
        type_name = match.group("type_name").lower()
        srid = int(match.group("srid"))
        dimensions = 3 if match.group("z") else 2
        geometry_type = next(
            (
                geometry_type
                for geometry_type, name in POSTGIS_GEOMETRY_TYPE_NAMES.items()
                if name.lower() == type_name
            ),
            None,
        )
        if geometry_type is None:
            return None
        field_kwargs: dict[str, Any] = {}
        if srid != DEFAULT_SRID:
            field_kwargs["srid"] = srid
        if is_geography:
            field_kwargs["geography"] = True
        if dimensions == 3:
            field_kwargs["dimensions"] = 3
        return f"hare.gis.fields.{GEOMETRY_FIELD_CLASS_NAMES[geometry_type]}", field_kwargs, False

    @classmethod
    async def fetch_catalog(cls, connection: DatabaseClient, tables: list[str], schema: str) -> CatalogSnapshot:
        """Read raw catalog records before any declaration inference."""
        return await PostgresqlCatalogReader.read_tables(connection, tables, schema)

    @classmethod
    def get_table_parts(cls, snapshot: CatalogSnapshot, table_infos: list[TableInfo]) -> list[SchemaPart]:
        return PostgresqlCatalogParts.get(snapshot, table_infos)
