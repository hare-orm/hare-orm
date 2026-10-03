from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.enums import ExclusionConstraintUsing, TriggerForEach, TriggerTiming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.triggers import Trigger
from hare.dialects.postgresql.constants import (
    ARRAY_ELEMENT_CHAR_LENGTH_RE,
    ARRAY_ELEMENT_NUMERIC_SIZE_RE,
    EXCLUSION_CONSTRAINT_DEF_RE,
    EXCLUSION_EXPRESSION_RE,
    EXCLUSION_RAW_EXPRESSION_RE,
    POSTGRES_CHECK_CONSTRAINT_DEF_RE,
    POSTGRES_FUNCTION_BODY_WRAPPER_RE,
    POSTGRES_FUNCTIONDEF_RE,
    POSTGRES_TRIGGERDEF_RE,
    POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP,
    POSTGRESQL_CANONICAL_TYPE_NAMES,
    POSTGRESQL_COLUMNS_SQL,
    POSTGRESQL_CONSTRAINTS_SQL,
    POSTGRESQL_DEFAULT_CHARACTER_LENGTH,
    POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES,
    POSTGRESQL_DEFAULT_SCHEMA,
    POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE,
    POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE,
    POSTGRESQL_FOREIGN_KEYS_SQL,
    POSTGRESQL_INDEXES_SQL,
    POSTGRESQL_NUMERIC_CAST_TYPES,
    POSTGRESQL_PARTITIONS_SQL,
    POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL,
    POSTGRESQL_SCHEMA_EXISTS_SQL,
    POSTGRESQL_TABLE_EXISTS_SQL,
    POSTGRESQL_TABLE_NAMES_SQL,
    POSTGRESQL_TABLES_SQL,
    POSTGRESQL_TRIGGERS_SQL,
    POSTGRESQL_TUNED_INDEX_TYPES,
    POSTGRESQL_TYPE_MAP,
    POSTGRESQL_TYPE_RE,
    VECTOR_DIMENSIONS_RE,
)
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
from hare.dialects.postgresql.partitioning.observed_partitioning import ObservedPartitioning
from hare.dialects.postgresql.table_options import PostgresqlTableOptions
from hare.fields.enums import OnDelete
from hare.inspectdb.constants import (
    AMBIGUOUS_REASON_SENTINEL_KWARG,
    BASE_FIELD_SENTINEL_KWARG,
)
from hare.inspectdb.exceptions import SchemaNotFoundError, TableNotFoundError
from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
from hare.inspectdb.types.column_info import ColumnInfo
from hare.inspectdb.types.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.types.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.types.index_info import IndexInfo
from hare.inspectdb.types.table_info import TableInfo
from hare.sql.enums import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlIntrospector(SchemaIntrospector):
    """Reads a PostgreSQL schema from its catalog (``pg_catalog`` and ``information_schema``) -
    every table of a request with one query per type of metadata, whatever the number of tables."""

    TYPE_MAP = POSTGRESQL_TYPE_MAP
    NOW_EXPRESSIONS = (*SchemaIntrospector.NOW_EXPRESSIONS, "statement_timestamp()")
    SEQUENCE_DEFAULT_PREFIXES = ("nextval(",)
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
        array_depth = match.group("array").count("[")
        return canonical_name + "[]" * array_depth

    @classmethod
    def column_types_differ(cls, declared_type: str, column: ColumnInfo) -> bool:
        """Whether both types normalize to different ``format_type()`` names - a spelling outside
        the known types is never reported.

        Args:
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            Whether they differ.
        """
        observed_type = cls.get_canonical_type(column.full_type or "")
        expected_type = cls.get_canonical_type(declared_type)
        return observed_type is not None and expected_type is not None and observed_type != expected_type

    @classmethod
    def get_type_mismatch_texts(cls, declared_type: str, column: ColumnInfo) -> tuple[str, str]:
        return column.full_type or column.db_type, cls.get_canonical_type(declared_type) or declared_type

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
        if column.db_type == "ARRAY":
            element_type_name = (column.udt_name or "").removeprefix("_")
            element_mapping = POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP.get(element_type_name)
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
                "hare.dialects.postgresql.fields.array.ArrayField",
                {BASE_FIELD_SENTINEL_KWARG: (element_path, element_kwargs)},
                False,
            )
        if column.db_type == "USER-DEFINED":
            if column.udt_name == "citext":
                # An extension type, but a real, unambiguous hare-orm field - unlike an arbitrary
                # enum/composite type.
                return "hare.dialects.postgresql.fields.citext.CitextField", {}, False
            if column.udt_name == "hstore":
                return "hare.dialects.postgresql.fields.hstore.HStoreField", {}, False
            if column.udt_name == "geography":
                # PostGIS's geography(Point,4326) - PostGISField's only supported shape.
                return "hare.dialects.postgresql.fields.gis.PostGISField", {}, False
            if column.udt_name == "vector" and column.vector_dimensions is not None:
                # VectorField's dimensions= is required - without a parsed dimension count this
                # falls through to the generic user-defined type below.
                return (
                    "hare.dialects.postgresql.fields.vector.VectorField",
                    {"dimensions": column.vector_dimensions},
                    False,
                )
            # A real enum (or other user-defined type) - reconstructing it as an enum field would
            # need its member values (pg_enum) too.
            udt_name = column.udt_name or "?"
            return (
                "hare.fields.data.text.TextField",
                {AMBIGUOUS_REASON_SENTINEL_KWARG: f"user-defined type {udt_name!r}"},
                True,
            )
        if lowered == "character":
            # Fixed-width, space-padded CHAR(n)/bpchar - CharField always generates VARCHAR(n), so
            # mapping it would silently change the column's semantics on the next migration diff.
            return "hare.fields.data.text.TextField", {}, True
        if "without time zone" in lowered:
            # DatetimeField always generates TIMESTAMPTZ - mapping a naive timestamp onto it would
            # silently change the column's semantics (naive -> tz-aware).
            return "hare.fields.data.text.TextField", {}, True
        return super().map_column_type(column)

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
        text - see _ARRAY_ELEMENT_NUMERIC_SIZE_RE's own comment for why information_schema alone
        can't report this for an ARRAY column.

        Returns:
            None for any column whose full_type doesn't match this exact shape.
        """
        match = ARRAY_ELEMENT_NUMERIC_SIZE_RE.match(full_type)
        return (int(match.group(1)), int(match.group(2))) if match else None

    @staticmethod
    def _parse_array_element_char_length(full_type: str) -> int | None:
        """Recovers a varchar[]/char[] column's own element length from format_type()'s
        "character varying(N)[]"/"character(N)[]" text - see _ARRAY_ELEMENT_CHAR_LENGTH_RE's own
        comment for why information_schema alone can't report this for an ARRAY column.

        Returns:
            None for any column whose full_type doesn't match this exact shape.
        """
        match = ARRAY_ELEMENT_CHAR_LENGTH_RE.match(full_type)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_storage_parameters(reloptions: list[str] | None) -> dict[str, str]:
        """Splits an index's ``pg_class.reloptions`` (``["lists=5", ...]``) into a dict.

        Args:
            reloptions: The raw ``name=value`` entries, or None when the index has none.

        Returns:
            Parameter name -> raw value text.
        """
        storage_parameters: dict[str, str] = {}
        for option in reloptions or ():
            name, _separator, value = option.partition("=")
            storage_parameters[name] = value
        return storage_parameters

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
        return [
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

    @staticmethod
    def _get_storage_parameter_value(text: str) -> bool | int | float | str:
        """Returns a table storage parameter's value as ``PostgresqlTableOptions`` declares it -
        ``pg_class.reloptions`` keeps each one as the text it was set with.

        Args:
            text: The value's text.

        Returns:
            An integer, a float or a boolean when the text spells one, the text otherwise.
        """
        for number_type in (int, float):
            try:
                return number_type(text)
            except ValueError:
                pass
        lowered_text = text.lower()
        if lowered_text in {"true", "on"}:
            return True
        if lowered_text in {"false", "off"}:
            return False
        return text

    @staticmethod
    def _get_storage_parameters(reloptions: list[str] | None) -> dict[str, bool | int | float | str]:
        """A table's storage parameters from its ``pg_class.reloptions``.

        Args:
            reloptions: The raw ``name=value`` entries, or None when the table has none.

        Returns:
            Parameter name -> value.
        """
        return {
            name: PostgresqlIntrospector._get_storage_parameter_value(value)
            for name, value in PostgresqlIntrospector._parse_storage_parameters(reloptions).items()
        }

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
        storage_parameters = PostgresqlIntrospector._get_storage_parameters(table_row["storage_parameters"])
        tablespace = table_row["tablespace"]
        partitions = [row for row in partition_rows if row["partition_table"] is not None]
        if partitions:
            storage_parameters = ObservedPartitioning.merge_partition_values(
                {
                    row["partition_table"]: PostgresqlIntrospector._get_storage_parameters(row["storage_parameters"])
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

    @classmethod
    def get_declared_table_options(
        cls,
        observed: TableOptions | None,
        declared: TableOptions | None,
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> TableOptions | None:
        """What the declared table options are compared as: a table naming the database's default
        tablespace is stored without one; a partitioning key is compared by its columns; the storage
        parameters of a partitioned table with no partition yet can't be seen, so the declared ones
        stand.
        """
        if observed is not None:
            observed = observed.with_field_names(column_to_field_name)
        if isinstance(observed, PostgresqlTableOptions) and isinstance(declared, PostgresqlTableOptions):
            observed_partitioning = observed.partitioning
            declared_partitioning = declared.with_field_names(column_to_field_name).partitioning
            if observed_partitioning is not None and declared.partitioning is not None:
                if declared_partitioning is not None and observed_partitioning.fields == declared_partitioning.fields:
                    observed_partitioning = dataclasses.replace(
                        observed_partitioning, fields=declared.partitioning.fields
                    )
                    observed = dataclasses.replace(observed, partitioning=observed_partitioning)
                if not observed_partitioning.get_partition_names():
                    observed = dataclasses.replace(observed, storage_parameters=declared.storage_parameters)
        if (
            isinstance(declared, PostgresqlTableOptions)
            and declared.tablespace is not None
            and declared.tablespace == table_info.default_tablespace
            and (observed is None or (isinstance(observed, PostgresqlTableOptions) and observed.tablespace is None))
        ):
            observed_options = observed if observed is not None else PostgresqlTableOptions()
            return dataclasses.replace(observed_options, tablespace=declared.tablespace)
        return observed

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
        pk_columns = {row["column_name"] for row in primary_key_rows}
        # The constraint's own declared column order, not physical table column order - see
        # ColumnInfo.pk_position.
        pk_positions = {row["column_name"]: row["ordinal_position"] for row in primary_key_rows}

        unique_columns: set[str] = set()
        indexed_columns: set[str] = set()
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        unparsed_indexes: list[tuple[str, str]] = []
        for row in index_rows:
            if row["is_primary"]:
                continue  # the primary key's own backing index, already covered by pk_columns
            storage_parameters = PostgresqlIntrospector._parse_storage_parameters(row["storage_parameters"])
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
                        is_unique=row["is_unique"],
                        name=row["index_name"],
                        index_type=index_type,
                        expression_terms=expression_terms,
                        storage_parameters=storage_parameters,
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(row["condition_sql"]),
                        include=list(row["include_columns"] or []),
                        nulls_not_distinct=row["nulls_not_distinct"],
                        unrepresentable_properties=PostgresqlIntrospector._get_postgres_unrepresentable_index_properties(
                            row, expression_terms
                        ),
                    )
                )
                continue
            index_columns = list(row["columns"])
            index_type = "" if row["index_type"] == "btree" else row["index_type"]
            opclasses = list(row["opclasses"]) if not all(row["opclass_is_default"]) else []
            default_opclasses = list(row["opclasses"]) if not opclasses else []
            # A single-column btree index with the default opclass and no condition becomes a field
            # flag; anything else needs Meta.indexes.
            key_orders = [
                Order.build(not is_descending, is_nulls_first).value
                for is_descending, is_nulls_first in zip(row["descending_keys"], row["nulls_first_keys"], strict=True)
            ]
            is_simple = (
                all(key_order == Order.ASC_NULLS_LAST for key_order in key_orders)
                and len(index_columns) == 1
                and not index_type
                and not opclasses
                and not row["condition_sql"]
                and not row["include_columns"]
                and not row["nulls_not_distinct"]
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
            )
            if is_simple:
                if row["is_unique"]:
                    unique_columns.add(index_columns[0])
                else:
                    indexed_columns.add(index_columns[0])
                column_indexes.append(index_info)
            else:
                indexes.append(index_info)

        columns = [
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
                db_default=(
                    PostgresqlIntrospector.parse_db_default(row["column_default"]) if row["column_default"] else None
                ),
                description=row["description"],
                udt_name=row["udt_name"],
                vector_dimensions=PostgresqlIntrospector._parse_vector_dimensions(row["udt_name"], row["full_type"]),
                array_element_numeric_precision_scale=PostgresqlIntrospector._parse_array_element_numeric_precision_scale(
                    row["full_type"]
                ),
                array_element_max_length=PostgresqlIntrospector._parse_array_element_char_length(row["full_type"]),
                # information_schema.columns reports a GENERATED ALWAYS AS (...) column's own
                # expression text directly, so parsing never fails here. Postgres only supports
                # STORED generated columns, so generated_stored keeps ColumnInfo's default True.
                generated_expression=row["generation_expression"] if row["is_generated"] == "ALWAYS" else None,
                identity_generation=row["identity_generation"] if row["is_identity"] == "YES" else None,
                full_type=row["full_type"],
            )
            for row in column_rows
        ]

        # A composite FOREIGN KEY becomes one ForeignKeyField when it references the target's whole
        # primary key under the shadow column names; otherwise its columns stay plain fields and it
        # is listed as unparsed. Its columns never become single-column foreign keys.
        composite_fk_columns: set[str] = set()
        composite_foreign_keys: list[CompositeForeignKeyInfo] = []
        unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = []
        for row in foreign_key_rows:
            member_columns = tuple(row["columns"])
            if len(member_columns) <= 1:
                continue
            composite_fk_columns.update(member_columns)
            match = SchemaIntrospector._match_composite_foreign_key_naming(
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
                )
            )

        foreign_keys = {}
        for row in foreign_key_rows:
            if len(row["columns"]) != 1 or row["columns"][0] in composite_fk_columns:
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
            )

        # pg_get_triggerdef()/pg_get_functiondef()'s canonical text is what
        # _parse_postgres_trigger_def() relies on being deterministic.
        triggers: list[Trigger] = []
        unparsed_triggers: list[tuple[str, str]] = []
        for row in trigger_rows:
            trigger = PostgresqlIntrospector._parse_postgres_trigger_def(
                row["name"], row["trigger_def"], row["function_def"]
            )
            if trigger is not None:
                triggers.append(trigger)
            else:
                unparsed_triggers.append((row["name"], row["trigger_def"]))

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
                exclusion_constraint = PostgresqlIntrospector._parse_postgres_exclusion_constraint_def(
                    row["conname"], row["definition"]
                )
                if exclusion_constraint is not None:
                    exclusion_constraints.append(exclusion_constraint)
                else:
                    unparsed_exclusion_constraints.append((row["conname"], row["definition"]))
                continue
            check_constraint = PostgresqlIntrospector._parse_postgres_check_constraint_def(
                row["conname"], row["definition"]
            )
            if check_constraint is not None:
                check_constraints.append(check_constraint)
                if row["definition"].rstrip().endswith(" NOT VALID"):
                    not_valid_constraint_names.append(row["conname"])
            else:
                unparsed_check_constraints.append((row["conname"], row["definition"]))

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
            default_tablespace=table_row["default_tablespace"],
        )

    @staticmethod
    def _get_postgres_unrepresentable_index_properties(row: dict[str, Any], key_terms: list[str]) -> list[str]:
        """Describes what an index has that no hare index or constraint can declare.

        Args:
            row: The index's POSTGRESQL_INDEXES_SQL row.
            key_terms: Each key's column name or expression text, in order.

        Returns:
            ``lower(a) DESC``/``lower(a) NULLS FIRST`` per key that has a sort order.
        """
        properties: list[str] = []
        for key_term, is_descending, is_nulls_first in zip(
            key_terms, row["descending_keys"], row["nulls_first_keys"], strict=False
        ):
            if is_descending:
                properties.append(f"{key_term} DESC" + ("" if is_nulls_first else " NULLS LAST"))
            elif is_nulls_first:
                properties.append(f"{key_term} NULLS FIRST")
        return properties

    @staticmethod
    def _parse_postgres_exclusion_constraint_def(name: str, definition: str) -> ExclusionConstraint | None:
        """Parses ``pg_get_constraintdef()``'s text of an EXCLUDE constraint. A term that isn't a plain
        column becomes a raw SQL term.

        Returns:
            The constraint, None when the text has another shape.
        """
        match = EXCLUSION_CONSTRAINT_DEF_RE.match(definition.strip())
        if not match:
            return None
        using = match.group("using")
        if using not in set(ExclusionConstraintUsing):
            return None
        terms = SchemaIntrospector.split_top_level_terms(match.group("expressions"))
        parsed_expressions: list[tuple[str | RawSQLTerm, str]] = []
        for term in terms:
            term_match = EXCLUSION_EXPRESSION_RE.match(term.strip())
            if term_match:
                parsed_expressions.append((term_match.group("field").strip('"'), term_match.group("operator")))
                continue
            raw_match = EXCLUSION_RAW_EXPRESSION_RE.match(term.strip())
            if not raw_match:
                return None
            parsed_expressions.append((RawSQLTerm(raw_match.group("expression")), raw_match.group("operator")))
        if not parsed_expressions:
            return None
        expressions = tuple(parsed_expressions)
        include = tuple(
            column.strip().strip('"') for column in (match.group("include") or "").split(",") if column.strip()
        )
        return ExclusionConstraint(
            name=name,
            expressions=expressions,
            using=ExclusionConstraintUsing(using),
            condition=RawSQLTerm(match.group("condition")) if match.group("condition") else None,
            include=include,
            deferrable=match.group("deferrable") is not None,
            initially_deferred=match.group("initially") == "DEFERRED",
        )

    @staticmethod
    def _parse_postgres_check_constraint_def(name: str, definition: str) -> CheckConstraint | None:
        """Best-effort parse of pg_get_constraintdef()'s own canonical text for a CHECK
        constraint. Returns None (surfaced as an unparsed-constraint comment instead) for
        anything not in the "CHECK (...) [NOT VALID]" shape."""
        match = POSTGRES_CHECK_CONSTRAINT_DEF_RE.match(definition.strip())
        if not match:
            return None
        # Postgres wraps the whole predicate in one more pair of parentheses than it was declared
        # with - "CHECK ((age >= 0))" for a declared "age >= 0".
        return CheckConstraint(
            name=name, check=RawSQLTerm(SchemaIntrospector.strip_outer_parentheses(match.group("expression")) or "")
        )

    @staticmethod
    def _parse_postgres_trigger_def(name: str, trigger_def: str, function_def: str) -> Trigger | None:
        """Rebuilds a ``Trigger`` from ``pg_get_triggerdef()``/``pg_get_functiondef()``, a constraint
        trigger's deferrability included.

        Returns:
            The trigger, None when either text has another shape.
        """
        trigger_match = POSTGRES_TRIGGERDEF_RE.match(trigger_def.strip())
        function_match = POSTGRES_FUNCTIONDEF_RE.search(function_def)
        if not trigger_match or not function_match:
            return None
        if trigger_match.group("from_table") is not None:
            # A constraint trigger's FROM other_table clause has no Trigger field to hold it -
            # reconstructing without it would silently produce a different trigger, not a
            # faithful (if incomplete) one, so this falls back to an unparsed-trigger comment.
            return None
        when = trigger_match.group("when")
        raw_body = function_match.group("body").strip()
        wrapper_match = POSTGRES_FUNCTION_BODY_WRAPPER_RE.match(raw_body)
        body = wrapper_match.group("inner").strip() if wrapper_match else raw_body
        # pg_get_triggerdef() prints NOT DEFERRABLE for a constraint trigger that isn't deferrable.
        is_constraint_trigger = bool(trigger_match.group("constraint"))
        deferrable = is_constraint_trigger and trigger_match.group("not_deferrable") is None
        initially_deferred = deferrable and trigger_match.group("initially") == "DEFERRED"
        return Trigger(
            name=name,
            on=SchemaIntrospector._normalize_event_clause(trigger_match.group("on")),
            body=body,
            # The regex only ever captures one of TriggerTiming's/TriggerForEach's own values, so
            # this conversion can't fail.
            timing=TriggerTiming(trigger_match.group("timing").upper()),
            for_each=TriggerForEach(trigger_match.group("for_each").upper()),
            when=when.strip() if when else None,
            language=(
                None
                if function_match.group("language").lower() == POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE
                else function_match.group("language").lower()
            ),
            deferrable=deferrable,
            initially_deferred=initially_deferred,
        )
