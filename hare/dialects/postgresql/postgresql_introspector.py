from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.enums import GrantTarget, RowLevelSecurity
from hare.ddl.schema_objects.enum_type import EnumType
from hare.ddl.schema_objects.trigger import Trigger
from hare.ddl.security.grant import Grant
from hare.dialects.postgresql.constants import (
    ARRAY_ELEMENT_CHAR_LENGTH_RE,
    ARRAY_ELEMENT_NUMERIC_SIZE_RE,
    NATIVE_ENUM_FIELD_PATH,
    POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP,
    POSTGRESQL_CANONICAL_TYPE_NAMES,
    POSTGRESQL_COLUMNS_SQL,
    POSTGRESQL_CONSTRAINTS_SQL,
    POSTGRESQL_DEFAULT_CHARACTER_LENGTH,
    POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES,
    POSTGRESQL_DEFAULT_SCHEMA,
    POSTGRESQL_DOMAIN_COLUMN_TODO,
    POSTGRESQL_ENUM_TYPES_SQL,
    POSTGRESQL_EXACT_TYPE_FIELD_PATHS,
    POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE,
    POSTGRESQL_EXTENSION_TYPE_FIELD_PATHS,
    POSTGRESQL_EXTENSIONS_SQL,
    POSTGRESQL_FIXED_CHAR_FIELD_PATH,
    POSTGRESQL_FOREIGN_KEYS_SQL,
    POSTGRESQL_INDEXES_SQL,
    POSTGRESQL_INSPECTED_COLUMN_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_FUNCTIONS_SQL,
    POSTGRESQL_INSPECTED_POLICIES_SQL,
    POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_SEQUENCES_SQL,
    POSTGRESQL_INSPECTED_TABLE_PRIVILEGES_SQL,
    POSTGRESQL_NAIVE_TYPE_FIELD_PATHS,
    POSTGRESQL_NUMERIC_CAST_TYPES,
    POSTGRESQL_PARTITIONS_SQL,
    POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL,
    POSTGRESQL_SCHEMA_EXISTS_SQL,
    POSTGRESQL_SCHEMA_NAMES_SQL,
    POSTGRESQL_TABLE_EXISTS_SQL,
    POSTGRESQL_TABLE_NAMES_SQL,
    POSTGRESQL_TABLES_SQL,
    POSTGRESQL_TRAILING_TYPE_CAST_RE,
    POSTGRESQL_TRIGGERS_SQL,
    POSTGRESQL_TS_VECTOR_FIELD_PATH,
    POSTGRESQL_TS_VECTOR_TYPE_NAME,
    POSTGRESQL_TUNED_INDEX_TYPES,
    POSTGRESQL_TYPE_MAP,
    POSTGRESQL_TYPE_RE,
    POSTGRESQL_UNATTACHED_SEQUENCES_SQL,
    POSTGRESQL_VIEWS_SQL,
    POSTGRESQL_VIRTUAL_GENERATED_COLUMN,
    VECTOR_DIMENSIONS_RE,
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
from hare.dialects.postgresql.introspection.postgresql_definitions import PostgresqlDefinitions
from hare.dialects.postgresql.introspection.postgresql_inspected_schema_objects import PostgresqlInspectedSchemaObjects
from hare.dialects.postgresql.introspection.postgresql_object_parts import PostgresqlObjectParts
from hare.dialects.postgresql.partitioning.observed_partitioning import ObservedPartitioning
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.postgresql.search.ts_vector_expression_reader import TSVectorExpressionReader
from hare.dialects.postgresql.spatial.constants import (
    POSTGIS_COLUMN_TYPE_PATTERN,
    POSTGIS_GEOGRAPHY_TYPE,
    POSTGIS_GEOMETRY_TYPE,
    POSTGIS_GEOMETRY_TYPE_NAMES,
)
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import OnDelete
from hare.gis.constants import DEFAULT_SRID, GEOMETRY_FIELD_CLASS_NAMES
from hare.inspectdb.constants import (
    AMBIGUOUS_REASON_SENTINEL_KWARG,
    BASE_FIELD_SENTINEL_KWARG,
    INSPECTED_ENUM_MODULE_NAME,
)
from hare.inspectdb.exceptions import SchemaNotFoundError, TableNotFoundError
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.inspectdb.introspection.table_info import TableInfo
from hare.sql.enums import Order

if TYPE_CHECKING:
    from hare.ddl.schema_objects.database_function import DatabaseFunction
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence
    from hare.dialects.base.client.database_client import DatabaseClient


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
    ) -> tuple[list[DatabaseSequence], list[DatabaseFunction], list[Grant]]:
        sequence_rows = await connection.execute_dicts(POSTGRESQL_UNATTACHED_SEQUENCES_SQL, [schema])
        function_rows = await connection.execute_dicts(POSTGRESQL_INSPECTED_FUNCTIONS_SQL, [schema])
        sequence_privilege_rows = await connection.execute_dicts(
            POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL, [schema]
        )
        function_privilege_rows = await connection.execute_dicts(
            POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL, [schema]
        )
        return (
            [PostgresqlInspectedSchemaObjects.get_sequence(row, None) for row in sequence_rows],
            [PostgresqlInspectedSchemaObjects.get_function(row) for row in function_rows],
            [
                *PostgresqlInspectedSchemaObjects.get_object_grants(sequence_privilege_rows, GrantTarget.SEQUENCE),
                *PostgresqlInspectedSchemaObjects.get_object_grants(function_privilege_rows, GrantTarget.FUNCTION),
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
        return await PostgresqlObjectParts.fetch(connection, schema, object_names)

    @classmethod
    async def fetch_tables(cls, connection: DatabaseClient, tables: list[str], schema: str) -> list[TableInfo]:
        return await PostgresqlIntrospector._inspect_tables_postgres(connection, tables, schema)

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
        match = POSTGRESQL_TYPE_RE.match(" ".join(type_sql.lower().split()))
        if match is None:
            return None
        type_name = match.group("name").strip()
        if match.group("time_zone"):
            type_name = f"{type_name} {' '.join(match.group('time_zone').split())}"
        canonical_name = POSTGRESQL_CANONICAL_TYPE_NAMES.get(type_name)
        if canonical_name is None:
            return None
        parameters = (match.group("parameters") or "").replace(" ", "")
        if canonical_name == "character" and not parameters:
            parameters = POSTGRESQL_DEFAULT_CHARACTER_LENGTH
        if parameters and canonical_name.startswith(("timestamp ", "time ")):
            # The precision goes before the time zone suffix: "timestamp(3) with time zone".
            base_name, _separator, time_zone = canonical_name.partition(" ")
            canonical_name = f"{base_name}({parameters}) {time_zone}"
        elif parameters:
            canonical_name = f"{canonical_name}({parameters})"
        # An array of any number of dimensions is one array type - the database keeps no count of them.
        return canonical_name + ("[]" if "[" in match.group("array") else "")

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
        kwargs = TSVectorExpressionReader.get_field_kwargs(
            cast("str", column.generated_expression), attribute_names_by_column
        )
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
        fields for (citext, hstore, PostGIS geography, pgvector) onto those, and the types a field
        would silently change the semantics of (fixed-width ``character``, naive ``timestamp``)
        onto an ambiguous TextField - the rest by ``TYPE_MAP``.

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
            if element_path == "hare.fields.data.text.CharField" and column.array_element_max_length is not None:
                element_kwargs["max_length"] = column.array_element_max_length
            if element_path == "hare.fields.data.numeric.DecimalField" and (
                precision_scale := column.array_element_numeric_precision_scale
            ):
                element_kwargs["max_digits"], element_kwargs["decimal_places"] = precision_scale
            return (
                "hare.fields.data.containers.array_field.ArrayField",
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
        """The field of a PostGIS column with a type and an SRID - ``geography(Point,4326)`` is the
        ``(latitude, longitude)`` PostGISField, any other a ``GeometryField`` of its type.

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
        if is_geography and type_name == "point" and srid == DEFAULT_SRID and dimensions == 2:
            return "hare.dialects.postgresql.fields.postgis_field.PostGISField", {}, False
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

    @staticmethod
    def _parse_vector_dimensions(udt_name: str | None, full_type: str) -> int | None:
        """Recovers a pgvector column's dimension count from format_type(atttypid, atttypmod)'s
        own "vector(N)" text - information_schema has no column for it, since vector is an
        extension type, not one it knows to introspect.

        Returns:
            None for any non-vector column, or a vector column whose dimension couldn't be parsed.
        """
        if udt_name != "vector":
            return None
        match = VECTOR_DIMENSIONS_RE.match(full_type)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_array_element_numeric_precision_scale(full_type: str) -> tuple[int, int] | None:
        """Recovers a numeric[] column's own precision/scale from format_type()'s "numeric(p,s)[]"
        text - information_schema reports no precision or scale for an ARRAY column.

        Returns:
            None for any column whose full_type doesn't match this exact shape.
        """
        match = ARRAY_ELEMENT_NUMERIC_SIZE_RE.match(full_type)
        return (int(match.group(1)), int(match.group(2))) if match else None

    @staticmethod
    def _parse_array_element_char_length(full_type: str) -> int | None:
        """Recovers a varchar[]/char[] column's own element length from format_type()'s
        "character varying(N)[]"/"character(N)[]" text - information_schema reports no length for an
        ARRAY column.

        Returns:
            None for any column whose full_type doesn't match this exact shape.
        """
        match = ARRAY_ELEMENT_CHAR_LENGTH_RE.match(full_type)
        return int(match.group(1)) if match else None

    @staticmethod
    async def _fetch_postgres_rows_by_table(
        connection: DatabaseClient, sql: str, tables: list[str], schema: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Runs one batched catalog query and groups its rows by their ``table_name``.

        Args:
            connection: The connection to read through.
            sql: The query - $1 is the schema, $2 the table names.
            tables: The tables to read.
            schema: The schema they live in.

        Returns:
            Table name -> its rows, in query order.
        """
        rows = await connection.execute_dicts(sql, [schema, tables])
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            rows_by_table.setdefault(row["table_name"], []).append(row)
        return rows_by_table

    @staticmethod
    async def _inspect_tables_postgres(connection: DatabaseClient, tables: list[str], schema: str) -> list[TableInfo]:
        """Reads every requested table's metadata with one catalog query per type of metadata.

        Args:
            connection: The connection to read through.
            tables: The tables to inspect.
            schema: The schema they live in.

        Returns:
            One TableInfo per entry of ``tables``, in the same order.

        Raises:
            TableNotFoundError: If a table doesn't exist.
        """
        if not tables:
            return []
        requested_tables = list(dict.fromkeys(tables))
        table_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_TABLES_SQL, requested_tables, schema
        )
        for table in requested_tables:
            if table not in table_rows_by_table:
                raise TableNotFoundError(table)
        primary_key_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL, requested_tables, schema
        )
        index_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_INDEXES_SQL, requested_tables, schema
        )
        column_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_COLUMNS_SQL, requested_tables, schema
        )
        foreign_key_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_FOREIGN_KEYS_SQL, requested_tables, schema
        )
        trigger_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_TRIGGERS_SQL, requested_tables, schema
        )
        constraint_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_CONSTRAINTS_SQL, requested_tables, schema
        )
        partition_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_PARTITIONS_SQL, requested_tables, schema
        )
        policy_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_INSPECTED_POLICIES_SQL, requested_tables, schema
        )
        table_privilege_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_INSPECTED_TABLE_PRIVILEGES_SQL, requested_tables, schema
        )
        column_privilege_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_INSPECTED_COLUMN_PRIVILEGES_SQL, requested_tables, schema
        )
        sequence_rows_by_table = await PostgresqlIntrospector._fetch_postgres_rows_by_table(
            connection, POSTGRESQL_INSPECTED_SEQUENCES_SQL, requested_tables, schema
        )
        table_infos = [
            PostgresqlIntrospector._build_postgres_table_info(
                table,
                schema,
                primary_key_rows=primary_key_rows_by_table.get(table, []),
                index_rows=index_rows_by_table.get(table, []),
                column_rows=column_rows_by_table.get(table, []),
                foreign_key_rows=foreign_key_rows_by_table.get(table, []),
                table_row=table_rows_by_table[table][0],
                trigger_rows=trigger_rows_by_table.get(table, []),
                constraint_rows=constraint_rows_by_table.get(table, []),
                partition_rows=partition_rows_by_table.get(table, []),
            )
            for table in tables
        ]
        for table_info in table_infos:
            table = table_info.name
            table_info.policies = PostgresqlInspectedSchemaObjects.get_policies(policy_rows_by_table.get(table, []))
            table_info.grants, table_info.unparsed_grants = PostgresqlInspectedSchemaObjects.get_grants(
                table_privilege_rows_by_table.get(table, []), column_privilege_rows_by_table.get(table, [])
            )
            table_info.sequences = [
                PostgresqlInspectedSchemaObjects.get_sequence(row, row["owner_column"])
                for row in sequence_rows_by_table.get(table, [])
            ]
        return table_infos

    @staticmethod
    def _get_table_options(
        table: str, table_row: dict[str, Any], partition_rows: list[dict[str, Any]]
    ) -> PostgresqlTableOptions | None:
        """Builds a table's ``PostgresqlTableOptions`` from its POSTGRESQL_TABLES_SQL row and, for a
        partitioned table, its POSTGRESQL_PARTITIONS_SQL rows: the storage parameters and the
        tablespace are those of its partitions - a value they don't share is named as such.

        Args:
            table: The table's name.
            table_row: The table's row.
            partition_rows: Its partition rows - none for a plain table.

        Returns:
            The options, None when every one has its default.
        """
        storage_parameters = PostgresqlDefinitions.get_storage_parameters(table_row["storage_parameters"])
        tablespace = table_row["tablespace"]
        partitions = [row for row in partition_rows if row["partition_table"] is not None]
        if partitions:
            storage_parameters = ObservedPartitioning.merge_partition_values(
                {
                    row["partition_table"]: PostgresqlDefinitions.get_storage_parameters(row["storage_parameters"])
                    for row in partitions
                }
            )
            default_tablespace = table_row["default_tablespace"]
            tablespaces = ObservedPartitioning.merge_partition_values(
                {
                    name: {"tablespace": partition_tablespace or default_tablespace}
                    for name, partition_tablespace in [
                        (table, tablespace),
                        *((row["partition_table"], row["tablespace"]) for row in partitions),
                    ]
                }
            )
            if tablespaces["tablespace"] != (tablespace or default_tablespace):
                tablespace = tablespaces["tablespace"]
        return PostgresqlTableOptions.from_observed(
            {
                "unlogged": bool(table_row["unlogged"]),
                "storage_parameters": storage_parameters,
                "tablespace": tablespace,
                "partitioning": ObservedPartitioning.build(table, partition_rows),
            }
        )

    @staticmethod
    def _build_postgres_table_info(
        table: str,
        schema: str,
        *,
        primary_key_rows: list[dict[str, Any]],
        index_rows: list[dict[str, Any]],
        column_rows: list[dict[str, Any]],
        foreign_key_rows: list[dict[str, Any]],
        table_row: dict[str, Any],
        trigger_rows: list[dict[str, Any]],
        constraint_rows: list[dict[str, Any]],
        partition_rows: list[dict[str, Any]],
    ) -> TableInfo:
        """Assembles one table's TableInfo from its rows of the batched catalog queries.

        Args:
            table: The table's name.
            schema: The schema it lives in.
            primary_key_rows: Its POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL rows.
            index_rows: Its POSTGRESQL_INDEXES_SQL rows.
            column_rows: Its POSTGRESQL_COLUMNS_SQL rows.
            foreign_key_rows: Its POSTGRESQL_FOREIGN_KEYS_SQL rows.
            table_row: Its POSTGRESQL_TABLES_SQL row - the comment and the storage.
            trigger_rows: Its POSTGRESQL_TRIGGERS_SQL rows.
            constraint_rows: Its POSTGRESQL_CONSTRAINTS_SQL rows.
            partition_rows: Its POSTGRESQL_PARTITIONS_SQL rows.

        Returns:
            The table's TableInfo.
        """
        indexes, column_indexes, unparsed_indexes, unique_columns, indexed_columns = (
            PostgresqlIntrospector._build_postgres_indexes(index_rows)
        )
        columns = PostgresqlIntrospector._build_postgres_columns(
            column_rows, primary_key_rows, unique_columns, indexed_columns
        )
        foreign_keys, composite_foreign_keys, unparsed_foreign_keys = (
            PostgresqlIntrospector._build_postgres_foreign_keys(foreign_key_rows, schema)
        )
        triggers, unparsed_triggers = PostgresqlIntrospector._build_postgres_triggers(trigger_rows)
        (
            exclusion_constraints,
            unparsed_exclusion_constraints,
            check_constraints,
            unparsed_check_constraints,
            not_valid_constraint_names,
        ) = PostgresqlIntrospector._build_postgres_constraints(constraint_rows)

        return TableInfo(
            name=table,
            schema=schema,
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            column_indexes=column_indexes,
            table_description=table_row["description"],
            triggers=triggers,
            unparsed_triggers=unparsed_triggers,
            exclusion_constraints=exclusion_constraints,
            unparsed_exclusion_constraints=unparsed_exclusion_constraints,
            composite_foreign_keys=composite_foreign_keys,
            unparsed_foreign_keys=unparsed_foreign_keys,
            unparsed_indexes=unparsed_indexes,
            check_constraints=check_constraints,
            unparsed_check_constraints=unparsed_check_constraints,
            not_valid_constraint_names=not_valid_constraint_names,
            is_in_default_schema=bool(table_row["is_default_schema"]),
            table_options=PostgresqlIntrospector._get_table_options(table, table_row, partition_rows),
            row_level_security=PostgresqlIntrospector.get_row_level_security(table_row),
            primary_key_without_overlaps=any(row["is_primary"] and row["without_overlaps"] for row in index_rows),
        )

    @staticmethod
    def get_column_db_default(column_default: str | None, is_pk: bool) -> Any:
        """A column's default - a sequence's next value only on a column outside the primary key: a
        generated key takes its own sequence, another column (SERIAL) the declared one.

        Args:
            column_default: The default's SQL, None for none.
            is_pk: Whether the column is in the primary key.

        Returns:
            The default.
        """
        if not column_default:
            return None
        if not is_pk and column_default.strip().lower().startswith(PostgresqlIntrospector.SEQUENCE_DEFAULT_PREFIXES):
            return SqlDefault(column_default.strip())
        return PostgresqlIntrospector.parse_db_default(column_default)

    @staticmethod
    def get_row_level_security(table_row: dict[str, Any]) -> RowLevelSecurity | None:
        """Whether row level security filters a table's rows, by its POSTGRESQL_TABLES_SQL row.

        Args:
            table_row: The row.

        Returns:
            ``FORCED`` when it filters the owner's rows too, ``ENABLED``, or None.
        """
        if not table_row["row_level_security"]:
            return None
        return RowLevelSecurity.FORCED if table_row["forced_row_level_security"] else RowLevelSecurity.ENABLED

    @staticmethod
    def _build_postgres_indexes(
        index_rows: list[dict[str, Any]],
    ) -> tuple[list[IndexInfo], list[IndexInfo], list[tuple[str, str]], set[str], set[str]]:
        """Builds a table's indexes - each an index of its own, a single column's (``index=True``,
        ``unique=True``) or one hare can't rebuild. The primary key's own index is left out.

        Args:
            index_rows: The table's POSTGRESQL_INDEXES_SQL rows.

        Returns:
            The indexes, the single columns' indexes, the unparsed indexes' names and definitions,
            and the unique and the indexed single columns.
        """
        unique_columns: set[str] = set()
        indexed_columns: set[str] = set()
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        unparsed_indexes: list[tuple[str, str]] = []
        for row in index_rows:
            if row["is_primary"]:
                continue  # the primary key's own backing index, already covered by pk_columns
            storage_parameters = PostgresqlDefinitions.parse_storage_parameters(row["storage_parameters"])
            if None in row["columns"] or any(row["key_collations"]):
                # An index with an expression term is rebuilt as Index(*expressions) of raw SQL
                # terms. Not when a term also has a non-default opclass - that index stays unparsed.
                expression_terms = (
                    [
                        f"{term} COLLATE {SchemaIntrospector.quote_identifier(collation)}" if collation else term
                        for term, collation in zip(row["term_sqls"], row["key_collations"], strict=True)
                    ]
                    if all(row["opclass_is_default"])
                    else None
                )
                if expression_terms is None or any(not term for term in expression_terms):
                    unparsed_indexes.append((row["index_name"], row["index_def"]))
                    continue
                index_type = "" if row["index_type"] == "btree" else row["index_type"]
                indexes.append(
                    IndexInfo(
                        columns=[],
                        key_orders=PostgresqlDefinitions.get_key_orders(row),
                        is_unique=row["is_unique"],
                        name=row["index_name"],
                        index_type=index_type,
                        expression_terms=expression_terms,
                        storage_parameters=storage_parameters,
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(row["condition_sql"]),
                        include=list(row["include_columns"] or []),
                        nulls_not_distinct=row["nulls_not_distinct"],
                    )
                )
                continue
            index_columns = list(row["columns"])
            index_type = "" if row["index_type"] == "btree" else row["index_type"]
            opclasses = list(row["opclasses"]) if not all(row["opclass_is_default"]) else []
            default_opclasses = list(row["opclasses"]) if not opclasses else []
            if row["without_overlaps"]:
                # A UNIQUE ... WITHOUT OVERLAPS constraint's GiST index and opclasses are its own.
                index_type, opclasses, default_opclasses = "", [], []
            # A single-column btree index with the default opclass and no condition becomes a field
            # flag; anything else needs Meta.indexes.
            key_orders = PostgresqlDefinitions.get_key_orders(row)
            is_simple = (
                all(key_order == Order.ASC_NULLS_LAST for key_order in key_orders)
                and len(index_columns) == 1
                and not index_type
                and not opclasses
                and not row["condition_sql"]
                and not row["include_columns"]
                and not row["nulls_not_distinct"]
                and not row["without_overlaps"]
            )
            index_info = IndexInfo(
                columns=index_columns,
                is_unique=row["is_unique"],
                name=row["index_name"],
                index_type=index_type,
                opclasses=opclasses,
                default_opclasses=default_opclasses,
                storage_parameters=storage_parameters,
                deferrable=row["is_deferrable"],
                initially_deferred=row["is_initially_deferred"],
                condition_sql=SchemaIntrospector.strip_outer_parentheses(row["condition_sql"]),
                include=list(row["include_columns"] or []),
                nulls_not_distinct=row["nulls_not_distinct"],
                key_orders=key_orders,
                without_overlaps=row["without_overlaps"],
            )
            if is_simple:
                if row["is_unique"]:
                    unique_columns.add(index_columns[0])
                else:
                    indexed_columns.add(index_columns[0])
                column_indexes.append(index_info)
            else:
                indexes.append(index_info)
        return indexes, column_indexes, unparsed_indexes, unique_columns, indexed_columns

    @staticmethod
    def _build_postgres_columns(
        column_rows: list[dict[str, Any]],
        primary_key_rows: list[dict[str, Any]],
        unique_columns: set[str],
        indexed_columns: set[str],
    ) -> list[ColumnInfo]:
        """Builds a table's columns.

        Args:
            column_rows: The table's POSTGRESQL_COLUMNS_SQL rows.
            primary_key_rows: The table's POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL rows.
            unique_columns: The columns of a single-column unique index.
            indexed_columns: The columns of a single-column index.

        Returns:
            The columns.
        """
        pk_columns = {row["column_name"] for row in primary_key_rows}
        # The constraint's own declared column order, not physical table column order - see
        # ColumnInfo.pk_position.
        pk_positions = {row["column_name"]: row["ordinal_position"] for row in primary_key_rows}

        return [
            ColumnInfo(
                name=row["column_name"],
                db_type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
                is_pk=row["column_name"] in pk_columns,
                pk_position=pk_positions.get(row["column_name"]),
                is_unique=row["column_name"] in unique_columns,
                has_index=row["column_name"] in indexed_columns,
                # Precision and scale are reported for every numeric type - used only once the
                # column is mapped to a DecimalField.
                max_length=row["character_maximum_length"],
                numeric_precision=row["numeric_precision"],
                numeric_scale=row["numeric_scale"],
                db_default=PostgresqlIntrospector.get_column_db_default(
                    row["column_default"], row["column_name"] in pk_columns
                ),
                description=row["description"],
                udt_name=row["udt_name"],
                vector_dimensions=PostgresqlIntrospector._parse_vector_dimensions(row["udt_name"], row["full_type"]),
                array_element_numeric_precision_scale=PostgresqlIntrospector._parse_array_element_numeric_precision_scale(
                    row["full_type"]
                ),
                array_element_max_length=PostgresqlIntrospector._parse_array_element_char_length(row["full_type"]),
                # information_schema.columns reports a GENERATED ALWAYS AS (...) column's own
                # expression text directly, so parsing never fails here.
                generated_expression=row["generation_expression"] if row["is_generated"] == "ALWAYS" else None,
                generated_stored=row["attgenerated"] != POSTGRESQL_VIRTUAL_GENERATED_COLUMN,
                identity_generation=row["identity_generation"] if row["is_identity"] == "YES" else None,
                full_type=row["full_type"],
                enum_labels=tuple(row["enum_labels"]) if row["enum_labels"] is not None else None,
                enum_type_schema=row["enum_type_schema"],
                todo_reason=None
                if row["domain_name"] is None
                else POSTGRESQL_DOMAIN_COLUMN_TODO.format(domain=row["domain_name"], type=row["data_type"]),
                generates_key=row["is_identity"] == "YES"
                or (row["column_default"] or "")
                .strip()
                .lower()
                .startswith(PostgresqlIntrospector.SEQUENCE_DEFAULT_PREFIXES),
            )
            for row in column_rows
        ]

    @staticmethod
    def _build_postgres_foreign_keys(
        foreign_key_rows: list[dict[str, Any]], schema: str
    ) -> tuple[dict[str, ForeignKeyInfo], list[CompositeForeignKeyInfo], list[tuple[str, tuple[str, ...]]]]:
        """Builds a table's foreign keys.

        Args:
            foreign_key_rows: The table's POSTGRESQL_FOREIGN_KEYS_SQL rows.
            schema: The schema the table lives in.

        Returns:
            The single-column foreign keys by column, the composite ones, and the names and columns
            of the composite ones hare can't declare.
        """
        # A composite FOREIGN KEY becomes one ForeignKeyField when it references the target's whole
        # primary key under the shadow column names; otherwise its columns stay plain fields and it
        # is listed as unparsed. Its columns never become single-column foreign keys.
        composite_foreign_key_columns: set[str] = set()
        composite_foreign_keys: list[CompositeForeignKeyInfo] = []
        unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = []
        for row in foreign_key_rows:
            member_columns = tuple(row["columns"])
            if len(member_columns) <= 1:
                continue
            composite_foreign_key_columns.update(member_columns)
            match = SchemaIntrospector.match_composite_foreign_key_naming(
                list(member_columns), list(row["target_columns"]), list(row["target_primary_key_columns"] or ())
            )
            # A relation can only name a model of this same run - one whose table is in this schema.
            if match is None or row["target_schema"] != schema:
                unparsed_foreign_keys.append((row["conname"], member_columns))
                continue
            field_name, ordered_columns = match
            composite_foreign_keys.append(
                CompositeForeignKeyInfo(
                    field_name=field_name,
                    columns=ordered_columns,
                    target_table=row["target_table"],
                    on_delete=(
                        OnDelete(row["delete_rule"]) if row["delete_rule"] in set(OnDelete) else OnDelete.CASCADE
                    ),
                    on_update=row["update_rule"],
                    deferral=SchemaIntrospector.get_deferral(row["is_deferrable"], row["is_initially_deferred"]),
                )
            )

        foreign_keys = {}
        for row in foreign_key_rows:
            if len(row["columns"]) != 1 or row["columns"][0] in composite_foreign_key_columns:
                continue
            column_name = row["columns"][0]
            target_column = row["target_columns"][0]
            # ForeignKeyFieldInstance already defaults to the target's PK, so to_field= is only
            # needed when the real target column is something else, e.g. a UNIQUE column.
            target_pk_columns = list(row["target_primary_key_columns"] or ())
            foreign_keys[column_name] = ForeignKeyInfo(
                column=column_name,
                target_table=row["target_table"],
                target_column=target_column,
                on_delete=OnDelete(row["delete_rule"]) if row["delete_rule"] in set(OnDelete) else OnDelete.CASCADE,
                to_field=target_column if target_pk_columns != [target_column] else None,
                target_schema=row["target_schema"],
                on_update=row["update_rule"],
                deferral=SchemaIntrospector.get_deferral(row["is_deferrable"], row["is_initially_deferred"]),
            )
        return foreign_keys, composite_foreign_keys, unparsed_foreign_keys

    @staticmethod
    def _build_postgres_triggers(trigger_rows: list[dict[str, Any]]) -> tuple[list[Trigger], list[tuple[str, str]]]:
        """Builds a table's triggers.

        Args:
            trigger_rows: The table's POSTGRESQL_TRIGGERS_SQL rows.

        Returns:
            The triggers, and the unparsed triggers' names and definitions.
        """
        # pg_get_triggerdef()/pg_get_functiondef()'s canonical text is what
        # PostgresqlDefinitions.parse_trigger() relies on being deterministic.
        triggers: list[Trigger] = []
        unparsed_triggers: list[tuple[str, str]] = []
        for row in trigger_rows:
            trigger = PostgresqlDefinitions.parse_trigger(row["name"], row["trigger_def"], row["function_def"])
            if trigger is not None:
                triggers.append(trigger if row["enabled"] else dataclasses.replace(trigger, enabled=False))
            else:
                unparsed_triggers.append((row["name"], row["trigger_def"]))
        return triggers, unparsed_triggers

    @staticmethod
    def _build_postgres_constraints(
        constraint_rows: list[dict[str, Any]],
    ) -> tuple[
        list[ExclusionConstraint], list[tuple[str, str]], list[CheckConstraint], list[tuple[str, str]], list[str]
    ]:
        """Builds a table's exclusion and CHECK constraints.

        Args:
            constraint_rows: The table's POSTGRESQL_CONSTRAINTS_SQL rows.

        Returns:
            The exclusion constraints, the unparsed ones' names and definitions, the CHECK
            constraints, the unparsed ones' names and definitions, and the names of the CHECK
            constraints added NOT VALID.
        """
        # pg_get_constraintdef()'s canonical text is regex-parsed, since the catalog splits an
        # exclusion constraint's per-column operators across several system columns with no single
        # structured view of "column WITH operator" pairs.
        exclusion_constraints: list[ExclusionConstraint] = []
        unparsed_exclusion_constraints: list[tuple[str, str]] = []
        check_constraints: list[CheckConstraint] = []
        unparsed_check_constraints: list[tuple[str, str]] = []
        not_valid_constraint_names: list[str] = []
        for row in constraint_rows:
            if row["constraint_type"] == POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE:
                exclusion_constraint = PostgresqlDefinitions.parse_exclusion_constraint(
                    row["conname"], row["definition"]
                )
                if exclusion_constraint is not None:
                    exclusion_constraints.append(exclusion_constraint)
                else:
                    unparsed_exclusion_constraints.append((row["conname"], row["definition"]))
                continue
            check_constraint = PostgresqlDefinitions.parse_check_constraint(row["conname"], row["definition"])
            if check_constraint is not None:
                check_constraints.append(check_constraint)
                if row["definition"].rstrip().endswith(" NOT VALID"):
                    not_valid_constraint_names.append(row["conname"])
            else:
                unparsed_check_constraints.append((row["conname"], row["definition"]))
        return (
            exclusion_constraints,
            unparsed_exclusion_constraints,
            check_constraints,
            unparsed_check_constraints,
            not_valid_constraint_names,
        )
