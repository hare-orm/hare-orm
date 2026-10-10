from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.enums import TriggerForEach, TriggerTiming
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.sqlite.constants import (
    SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME,
    SPATIALITE_INDEX_TYPE,
    SPATIALITE_INDEXED_COLUMNS_SQL,
    SPATIALITE_METADATA_MARKER_TABLE_NAMES,
    SPATIALITE_METADATA_TABLE_NAMES,
    SPATIALITE_TRIGGER_NAME_PREFIXES,
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_CHECK_CONSTRAINT_HEADER_RE,
    SQLITE_DEFAULT_TYPE_AFFINITY,
    SQLITE_EARLIER_COLUMN_TYPES,
    SQLITE_EMPTY_TYPE_AFFINITY,
    SQLITE_FULL_TEXT_ARGUMENT_PATTERN,
    SQLITE_FULL_TEXT_CONTENT_OPTION,
    SQLITE_FULL_TEXT_CONTENT_ROWID_OPTION,
    SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX,
    SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX,
    SQLITE_FULL_TEXT_MODULE,
    SQLITE_FULL_TEXT_TABLE_PATTERN,
    SQLITE_FULL_TEXT_TOKENIZE_OPTION,
    SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX,
    SQLITE_INDEX_ON_RE,
    SQLITE_INDEX_TERM_COLLATE_RE,
    SQLITE_INDEX_TERM_MODIFIER_RE,
    SQLITE_INDEX_TERM_ORDER_RE,
    SQLITE_INDEX_WHERE_RE,
    SQLITE_MAIN_SCHEMA,
    SQLITE_NOW_UTC_SQL,
    SQLITE_PRIMARY_KEY_INDEX_ORIGIN,
    SQLITE_SIZE_RE,
    SQLITE_TABLE_EXISTS_SQL,
    SQLITE_TABLE_OPTIONS_PATTERN,
    SQLITE_TABLE_UNIQUE_CONSTRAINT_RE,
    SQLITE_TRIGGERDEF_RE,
    SQLITE_TYPE_AFFINITY_RULES,
    SQLITE_TYPE_MAP,
    SQLITE_VIRTUAL_TABLES_SQL,
)
from hare.dialects.sqlite.indexes.full_text_index import FullTextIndex
from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import OnDelete
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.sql.enums import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient


class SqliteIntrospector(SchemaIntrospector):
    """Reads a SQLite schema from ``sqlite_master`` and the ``PRAGMA`` table functions - the
    declared ``CREATE`` text is where SQLite keeps what its pragmas don't report (constraint
    names, generated column expressions, index conditions)."""

    TYPE_MAP = SQLITE_TYPE_MAP
    INDEX_CLASSES_BY_TYPE: ClassVar[Mapping[str, type[Index]]] = {
        SQLITE_FULL_TEXT_MODULE: FullTextIndex,
        SPATIALITE_INDEX_TYPE: SpatialiteIndex,
    }
    # SQLite echoes a DEFAULT (expression) without its outer parentheses - Now()'s own
    # higher-precision expression is read the same way as the plain CURRENT_TIMESTAMP.
    NOW_EXPRESSIONS = (
        *SchemaIntrospector.NOW_EXPRESSIONS,
        SQLITE_NOW_UTC_SQL.removeprefix("(").removesuffix(")"),
    )

    @classmethod
    def is_unnamed_index_name(cls, name: str, table_name: str, column_names: Sequence[str]) -> bool:
        """The index backing a UNIQUE constraint is an ``sqlite_autoindex_<table>_<n>``."""
        return super().is_unnamed_index_name(name, table_name, column_names) or name.startswith(
            SQLITE_AUTOINDEX_PREFIX
        )

    @classmethod
    async def fetch_default_schema(cls, connection: DatabaseClient) -> str:
        return SQLITE_MAIN_SCHEMA

    @classmethod
    async def fetch_table_names(cls, connection: DatabaseClient, schema: str, include_partitions: bool) -> list[str]:
        # One schema, no partitions. A virtual table - a FullTextIndex's FTS5 table among them -
        # and the shadow tables keeping its data, named after it, are no model's.
        rows = await connection.execute_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        virtual_table_names = [row["name"] for row in await connection.execute_dicts(SQLITE_VIRTUAL_TABLES_SQL)]
        table_names = [
            row["name"]
            for row in rows
            if not any(
                row["name"] == virtual_table_name or row["name"].startswith(f"{virtual_table_name}_")
                for virtual_table_name in virtual_table_names
            )
        ]
        # SpatiaLite's spatial metadata is no model's either.
        if set(table_names) >= SPATIALITE_METADATA_MARKER_TABLE_NAMES:
            table_names = [name for name in table_names if name not in SPATIALITE_METADATA_TABLE_NAMES]
        return table_names

    @staticmethod
    def get_full_text_index_info(table: str, index_table_name: str, index_table_sql: str) -> IndexInfo | None:
        """Reads a ``FullTextIndex`` of a table off its FTS5 table's CREATE text.

        Args:
            table: The indexed table.
            index_table_name: The FTS5 table.
            index_table_sql: Its CREATE VIRTUAL TABLE text.

        Returns:
            The index - its type ``fts5``, its tokenizer a storage parameter; None for a virtual
            table that is no FTS5 index reading ``table`` by its integer key, as a FullTextIndex is.
        """
        table_match = SQLITE_FULL_TEXT_TABLE_PATTERN.match(index_table_sql)
        if table_match is None:
            return None
        columns: list[str] = []
        options: dict[str, str] = {}
        arguments_sql = table_match["arguments"]
        position = 0
        while position < len(arguments_sql):
            argument_match = SQLITE_FULL_TEXT_ARGUMENT_PATTERN.match(arguments_sql, position)
            if argument_match is None or argument_match.end() == position:
                return None
            position = argument_match.end()
            if argument_match["option"] is not None:
                value = argument_match["value"].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1].replace(value[0] * 2, value[0])
                options[argument_match["option"].lower()] = value
            elif argument_match["modifiers"].strip():
                # An UNINDEXED column - FullTextIndex indexes every column it reads.
                return None
            else:
                columns.append(SqliteIntrospector._unquote_sqlite_identifier(argument_match["column"]))
        if (
            not columns
            or options.get(SQLITE_FULL_TEXT_CONTENT_OPTION) != table
            or SQLITE_FULL_TEXT_CONTENT_ROWID_OPTION not in options
        ):
            return None
        tokenizer = options.get(SQLITE_FULL_TEXT_TOKENIZE_OPTION)
        return IndexInfo(
            columns=columns,
            is_unique=False,
            name=index_table_name,
            index_type=SQLITE_FULL_TEXT_MODULE,
            storage_parameters={} if tokenizer is None else {"tokenizer": tokenizer},
        )

    @staticmethod
    async def fetch_full_text_indexes(connection: DatabaseClient, table: str) -> list[IndexInfo]:
        """The ``FullTextIndex``es of a table - the FTS5 tables reading it.

        Args:
            connection: The connection.
            table: The table.

        Returns:
            The indexes.
        """
        full_text_indexes = []
        for row in await connection.execute_dicts(SQLITE_VIRTUAL_TABLES_SQL):
            index_info = SqliteIntrospector.get_full_text_index_info(table, row["name"], row["sql"])
            if index_info is not None:
                full_text_indexes.append(index_info)
        return full_text_indexes

    @staticmethod
    async def fetch_spatial_indexes(
        connection: DatabaseClient, table: str, column_names: Sequence[str]
    ) -> list[IndexInfo]:
        """The ``SpatialiteIndex``es of a table - its columns SpatiaLite's spatial metadata has an
        enabled spatial index of. The metadata keeps names lowercased; each is read back as the
        table names its column.

        Args:
            connection: The connection.
            table: The table.
            column_names: The table's columns.

        Returns:
            The indexes - unnamed: SpatiaLite names their tables.
        """
        if not await SqliteIntrospector.fetch_table_exists(connection, SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME):
            return []
        column_names_by_lowercase_name = {column_name.lower(): column_name for column_name in column_names}
        spatial_indexes = []
        for row in await connection.execute_dicts(SPATIALITE_INDEXED_COLUMNS_SQL, [table]):
            column_name = column_names_by_lowercase_name.get(row["f_geometry_column"].lower())
            if column_name is not None:
                spatial_indexes.append(
                    IndexInfo(columns=[column_name], is_unique=False, index_type=SPATIALITE_INDEX_TYPE)
                )
        return spatial_indexes

    @classmethod
    async def fetch_table_exists(cls, connection: DatabaseClient, table: str) -> bool:
        _, rows = await connection.execute(SQLITE_TABLE_EXISTS_SQL, [table])
        return bool(rows)

    @classmethod
    async def fetch_tables(cls, connection: DatabaseClient, tables: list[str], schema: str) -> list[TableInfo]:
        return [await SqliteIntrospector._inspect_table_sqlite(connection, table) for table in tables]

    @staticmethod
    def get_type_affinity(type_sql: str) -> str:
        """The type affinity SQLite gives a column declared with ``type_sql``.

        Args:
            type_sql: The declared type.

        Returns:
            INTEGER, TEXT, BLOB, REAL or NUMERIC.
        """
        upper_type_sql = type_sql.upper()
        if not upper_type_sql.strip():
            return SQLITE_EMPTY_TYPE_AFFINITY
        for substrings, affinity in SQLITE_TYPE_AFFINITY_RULES:
            if any(substring in upper_type_sql for substring in substrings):
                return affinity
        return SQLITE_DEFAULT_TYPE_AFFINITY

    @classmethod
    def column_types_differ(cls, declared_type: str, column: ColumnInfo) -> bool:
        """Whether the two types have different affinities - SQLite stores a value by its column's
        affinity, whatever else the declared type spells; a column created under an earlier name of
        the declared type counts as that type.

        Args:
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            Whether they differ.
        """
        if column.db_type.upper() in SQLITE_EARLIER_COLUMN_TYPES.get(declared_type.upper(), ()):
            return False
        return cls.get_type_affinity(declared_type) != cls.get_type_affinity(column.db_type)

    @classmethod
    def get_type_mismatch_texts(cls, declared_type: str, column: ColumnInfo) -> tuple[str, str]:
        return column.db_type, declared_type

    @classmethod
    def map_column_type(cls, column: ColumnInfo) -> tuple[str, dict[str, Any], bool]:
        if column.db_type.lower().startswith("char") and column.max_length == 36:
            # No native UUID type - UUIDField declares one as plain "CHAR(36)", indistinguishable
            # from a genuine CharField(max_length=36) at the schema level.
            return "hare.fields.data.uuid_field.UUIDField", {}, False
        return super().map_column_type(column)

    @staticmethod
    def _parse_sqlite_type_size(db_type: str) -> tuple[int | None, int | None]:
        """Returns (first_number, second_number) parsed out of a declared type string like
        "VARCHAR(255)" or "DECIMAL(10, 2)" - (None, None) if the type has no parenthesized size at
        all (a bare "TEXT"/"INT", most commonly)."""
        match = SQLITE_SIZE_RE.search(db_type)
        if not match:
            return None, None
        first = int(match.group(1))
        second = int(match.group(2)) if match.group(2) is not None else None
        return first, second

    @staticmethod
    def _parse_sqlite_db_default(raw_sql: str) -> Any:
        """Like parse_db_default, for SQLite's PRAGMA table_info text.

        SQLite reports an expression default declared as ``DEFAULT (expr)`` without its outer
        parentheses, but only accepts an expression other than a literal inside them - an
        unrecognized expression gets them back.

        Args:
            raw_sql: The PRAGMA dflt_value text.

        Returns:
            The parsed default.
        """
        db_default = SqliteIntrospector.parse_db_default(raw_sql)
        if type(db_default) is SqlDefault:
            return SqlDefault(f"({db_default.sql})")
        return db_default

    @staticmethod
    async def _inspect_table_sqlite(connection: DatabaseClient, table: str) -> TableInfo:
        # PRAGMA takes no parameters - `table` was checked against sqlite_master by
        # inspect_tables().
        # table_xinfo, not table_info, which doesn't list generated columns. hidden: 0 normal, 1 a
        # virtual table's hidden column (skipped), 2 VIRTUAL generated, 3 STORED generated.
        raw_column_rows = await connection.execute_dicts(
            f"PRAGMA table_xinfo({SchemaIntrospector.quote_identifier(table)})"
        )
        column_rows = [row for row in raw_column_rows if row["hidden"] != 1]

        # The CREATE TABLE text as written - the only source of a generated column's expression and
        # of CHECK constraints.
        table_sql_rows = await connection.execute_dicts(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", [table]
        )
        table_sql = table_sql_rows[0]["sql"] if table_sql_rows else ""

        (
            indexes,
            column_indexes,
            unparsed_indexes,
            unique_columns,
            indexed_columns,
        ) = await SqliteIntrospector._read_sqlite_indexes(connection, table, table_sql)
        columns, unparsed_generated_columns = SqliteIntrospector._read_sqlite_columns(
            column_rows, table_sql, unique_columns, indexed_columns
        )
        (
            foreign_keys,
            composite_foreign_keys,
            unparsed_foreign_keys,
        ) = await SqliteIntrospector._read_sqlite_foreign_keys(connection, table)
        extension_indexes, index_trigger_names = await SqliteIntrospector._read_sqlite_extension_indexes(
            connection, table, [row["name"] for row in column_rows]
        )
        indexes.extend(extension_indexes)
        triggers, unparsed_triggers = await SqliteIntrospector._read_sqlite_triggers(
            connection, table, index_trigger_names
        )

        check_constraints, unparsed_check_constraints = SqliteIntrospector._parse_sqlite_check_constraints(
            table, table_sql
        )

        return TableInfo(
            name=table,
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            column_indexes=column_indexes,
            unparsed_indexes=unparsed_indexes,
            triggers=triggers,
            unparsed_triggers=unparsed_triggers,
            composite_foreign_keys=composite_foreign_keys,
            unparsed_foreign_keys=unparsed_foreign_keys,
            unparsed_generated_columns=unparsed_generated_columns,
            check_constraints=check_constraints,
            unparsed_check_constraints=unparsed_check_constraints,
            table_options=SqliteIntrospector._get_table_options(connection, table_sql),
        )

    @staticmethod
    async def _read_sqlite_indexes(
        connection: DatabaseClient, table: str, table_sql: str
    ) -> tuple[list[IndexInfo], list[IndexInfo], list[tuple[str, str]], set[str], set[str]]:
        """Reads a table's indexes - each an index of its own, a single column's (``index=True``,
        ``unique=True``) or one hare can't rebuild.

        Args:
            connection: The connection.
            table: The table.
            table_sql: The table's CREATE TABLE text.

        Returns:
            The indexes, the single columns' indexes, the unparsed indexes' names and SQL, and the
            unique and the indexed single columns.
        """
        index_rows = await connection.execute_dicts(f"PRAGMA index_list({SchemaIntrospector.quote_identifier(table)})")

        # sqlite_master.sql stores each index's original, literal CREATE INDEX text - the only
        # source of an expression term's text and of a partial index's WHERE predicate.
        index_sql_rows = await connection.execute_dicts(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", [table]
        )
        index_sql_by_name = {row["name"]: row["sql"] for row in index_sql_rows}

        unique_columns: set[str] = set()
        indexed_columns: set[str] = set()
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        unparsed_indexes: list[tuple[str, str]] = []
        unique_constraint_names_by_columns = SqliteIntrospector._parse_sqlite_unique_constraint_names(table_sql)
        for index_row in index_rows:
            index_column_rows = await connection.execute_dicts(
                f"PRAGMA index_info({SchemaIntrospector.quote_identifier(index_row['name'])})"
            )
            index_columns = [row["name"] for row in index_column_rows]
            if index_row["origin"] == SQLITE_PRIMARY_KEY_INDEX_ORIGIN:
                # Backing the primary key itself, already covered by ColumnInfo.is_pk - an index
                # created on the same columns is an index of its own.
                continue
            is_unique = bool(index_row["unique"])
            index_name = index_row["name"]
            constraint_names = unique_constraint_names_by_columns.get(tuple(index_columns))
            if index_name.startswith(SQLITE_AUTOINDEX_PREFIX) and constraint_names:
                index_name = constraint_names.pop(0)
            raw_sql = index_sql_by_name.get(index_row["name"])
            # An implicit autoindex (UNIQUE constraint) has no `sql` at all, but is never partial
            # and never has an expression term either.
            index_terms, raw_condition = (
                SqliteIntrospector._parse_sqlite_index_definition(raw_sql) if raw_sql else (None, None)
            )
            if index_row["partial"] and raw_condition is None:
                if raw_sql:
                    unparsed_indexes.append((index_row["name"], raw_sql))
                continue
            if None in index_columns:
                # At least one index term is an expression, not a plain column (PRAGMA
                # index_info reports a NULL name for it) - rebuilt as Index(*RawSQLTerm) from the
                # literal CREATE INDEX text, unless a term carries a sort order/collation.
                if index_terms is None or any(SQLITE_INDEX_TERM_MODIFIER_RE.search(term) for term in index_terms):
                    if raw_sql:
                        unparsed_indexes.append((index_row["name"], raw_sql))
                    continue
                indexes.append(
                    IndexInfo(
                        columns=[],
                        is_unique=is_unique,
                        name=index_row["name"],
                        expression_terms=index_terms,
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(raw_condition),
                    )
                )
                continue
            key_orders, collated_terms, unrepresentable_properties = SqliteIntrospector._get_sqlite_key_details(
                index_terms or []
            )
            if collated_terms is not None:
                # A collated key can only be declared as an expression, e.g. Collate("title", "NOCASE").
                indexes.append(
                    IndexInfo(
                        columns=[],
                        is_unique=is_unique,
                        name=index_row["name"],
                        expression_terms=collated_terms,
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(raw_condition),
                    )
                )
                continue
            if raw_condition is not None:
                indexes.append(
                    IndexInfo(
                        columns=index_columns,
                        is_unique=is_unique,
                        name=index_row["name"],
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(raw_condition),
                        unrepresentable_properties=unrepresentable_properties,
                        key_orders=key_orders,
                    )
                )
                continue
            index_info = IndexInfo(
                columns=index_columns,
                is_unique=is_unique,
                name=index_name,
                unrepresentable_properties=unrepresentable_properties,
                key_orders=key_orders,
            )
            if len(index_columns) == 1 and all(key_order == Order.ASC_NULLS_LAST for key_order in key_orders):
                if is_unique:
                    unique_columns.add(index_columns[0])
                else:
                    indexed_columns.add(index_columns[0])
                column_indexes.append(index_info)
            else:
                indexes.append(index_info)

        return indexes, column_indexes, unparsed_indexes, unique_columns, indexed_columns

    @staticmethod
    def _read_sqlite_columns(
        column_rows: list[dict[str, Any]], table_sql: str, unique_columns: set[str], indexed_columns: set[str]
    ) -> tuple[list[ColumnInfo], list[tuple[str, str]]]:
        """The columns of a table's ``PRAGMA table_xinfo`` rows - a generated column whose expression
        can't be parsed out of the CREATE TABLE text is left out and listed as unparsed.

        Args:
            column_rows: The rows, a virtual table's hidden columns left out.
            table_sql: The table's CREATE TABLE text.
            unique_columns: The columns of a single-column unique index.
            indexed_columns: The columns of a single-column index.

        Returns:
            The columns, and the unparsed generated columns' names with the table's SQL.
        """
        columns = []
        unparsed_generated_columns: list[tuple[str, str]] = []
        for row in column_rows:
            length, scale = SqliteIntrospector._parse_sqlite_type_size(row["type"])
            is_decimal_like = "numeric" in row["type"].lower() or "decimal" in row["type"].lower()
            # A generated column's expression is parsed out of the CREATE TABLE text; when it can't
            # be, the column is left out and listed as unparsed rather than kept as an ordinary
            # field.
            is_generated = row["hidden"] in {2, 3}
            generated_expression = (
                SqliteIntrospector._parse_sqlite_generated_column_expression(table_sql, row["name"])
                if is_generated
                else None
            )
            if is_generated and generated_expression is None:
                unparsed_generated_columns.append((row["name"], table_sql))
                continue
            columns.append(
                ColumnInfo(
                    name=row["name"],
                    db_type=row["type"],
                    nullable=not row["notnull"],
                    is_pk=bool(row["pk"]),
                    # PRAGMA table_xinfo's own "pk" value IS already the 1-based declared
                    # position within the PK (0 for a non-PK column) - see ColumnInfo.pk_position.
                    pk_position=row["pk"] or None,
                    is_unique=row["name"] in unique_columns,
                    has_index=row["name"] in indexed_columns,
                    max_length=None if is_decimal_like else length,
                    numeric_precision=length if is_decimal_like else None,
                    numeric_scale=scale if is_decimal_like else None,
                    db_default=(
                        SqliteIntrospector._parse_sqlite_db_default(row["dflt_value"])
                        if row["dflt_value"] is not None
                        else None
                    ),
                    generated_expression=generated_expression,
                    generated_stored=row["hidden"] == 3,
                )
            )

        return columns, unparsed_generated_columns

    @staticmethod
    async def _read_sqlite_foreign_keys(
        connection: DatabaseClient, table: str
    ) -> tuple[dict[str, ForeignKeyInfo], list[CompositeForeignKeyInfo], list[tuple[str, tuple[str, ...]]]]:
        """Reads a table's foreign keys.

        Args:
            connection: The connection.
            table: The table.

        Returns:
            The single-column foreign keys by column, the composite ones, and the names and columns
            of the composite ones not following the shadow column naming.
        """
        # foreign_key_list's "id" groups the rows of one constraint, "seq" orders its columns. A
        # composite one becomes one ForeignKeyField when its columns follow the shadow column
        # naming, otherwise its columns stay plain fields and it is listed as unparsed.
        foreign_key_rows = await connection.execute_dicts(
            f"PRAGMA foreign_key_list({SchemaIntrospector.quote_identifier(table)})"
        )
        foreign_key_rows_by_id: dict[int, list[dict[str, Any]]] = {}
        for row in foreign_key_rows:
            foreign_key_rows_by_id.setdefault(row["id"], []).append(row)

        foreign_keys: dict[str, ForeignKeyInfo] = {}
        composite_foreign_keys: list[CompositeForeignKeyInfo] = []
        unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = []
        for foreign_key_id, rows in foreign_key_rows_by_id.items():
            # ForeignKeyFieldInstance already defaults to the target's PK, so to_field= is only
            # needed when the real target column ("to") is something else, e.g. a UNIQUE column.
            target_pk_rows = await connection.execute_dicts(
                f"PRAGMA table_xinfo({SchemaIntrospector.quote_identifier(rows[0]['table'])})"
            )
            target_pk_columns = [
                target_row["name"]
                for target_row in sorted(target_pk_rows, key=lambda row: row["pk"])
                if target_row["pk"]
            ]
            if len(rows) > 1:
                ordered_rows = sorted(rows, key=lambda row: row["seq"])
                member_columns = tuple(row["from"] for row in ordered_rows)
                # "to" is NULL for a bare "REFERENCES target" - that names the target's own PK.
                target_columns = (
                    [row["to"] for row in ordered_rows]
                    if all(row["to"] is not None for row in ordered_rows)
                    else target_pk_columns
                )
                match = (
                    SchemaIntrospector.match_composite_foreign_key_naming(
                        list(member_columns), target_columns, target_pk_columns
                    )
                    if len(target_columns) == len(member_columns)
                    else None
                )
                if match is None:
                    unparsed_foreign_keys.append((f"{table}_fk_{foreign_key_id}", member_columns))
                    continue
                field_name, ordered_columns = match
                composite_foreign_keys.append(
                    CompositeForeignKeyInfo(
                        field_name=field_name,
                        columns=ordered_columns,
                        target_table=ordered_rows[0]["table"],
                        on_delete=(
                            OnDelete(ordered_rows[0]["on_delete"])
                            if ordered_rows[0]["on_delete"] in set(OnDelete)
                            else OnDelete.CASCADE
                        ),
                    )
                )
                continue
            row = rows[0]
            foreign_keys[row["from"]] = ForeignKeyInfo(
                column=row["from"],
                target_table=row["table"],
                target_column=row["to"],
                on_delete=OnDelete(row["on_delete"]) if row["on_delete"] in set(OnDelete) else OnDelete.CASCADE,
                to_field=row["to"] if target_pk_columns != [row["to"]] else None,
            )
        return foreign_keys, composite_foreign_keys, unparsed_foreign_keys

    @staticmethod
    async def _read_sqlite_extension_indexes(
        connection: DatabaseClient, table: str, column_names: list[str]
    ) -> tuple[list[IndexInfo], set[str]]:
        """Reads the indexes of a table an extension keeps - FTS5's full-text indexes, SpatiaLite's
        spatial ones - with the names of the triggers keeping them, which belong to the indexes.

        Args:
            connection: The connection.
            table: The table.
            column_names: The table's column names.

        Returns:
            The indexes and the names of their triggers, lower-cased for a spatial index.
        """
        indexes: list[IndexInfo] = []
        # A FullTextIndex is an index of the table, its triggers part of it.
        full_text_indexes = await SqliteIntrospector.fetch_full_text_indexes(connection, table)
        indexes.extend(full_text_indexes)
        index_trigger_names = {
            index.name + suffix
            for index in full_text_indexes
            for suffix in (
                SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX,
                SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX,
                SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX,
            )
        }
        # So is a SpatialiteIndex - its triggers SpatiaLite's own.
        spatial_indexes = await SqliteIntrospector.fetch_spatial_indexes(connection, table, column_names)
        indexes.extend(spatial_indexes)
        index_trigger_names.update(
            f"{prefix}_{table}_{index.columns[0]}".lower()
            for index in spatial_indexes
            for prefix in SPATIALITE_TRIGGER_NAME_PREFIXES
        )
        return indexes, index_trigger_names

    @staticmethod
    async def _read_sqlite_triggers(
        connection: DatabaseClient, table: str, index_trigger_names: set[str]
    ) -> tuple[list[Trigger], list[tuple[str, str]]]:
        """Reads a table's triggers, those of its extensions' indexes left out.

        Args:
            connection: The connection.
            table: The table.
            index_trigger_names: The names of the triggers of the table's extension indexes.

        Returns:
            The triggers, and the unparsed triggers' names and SQL.
        """
        # sqlite_master.sql stores the trigger's original, literal CREATE TRIGGER text exactly as
        # written (SQLite has no equivalent of Postgres's pg_get_triggerdef() to canonicalize it
        # first), so _parse_sqlite_trigger_def() has to tolerate more formatting variance.
        trigger_rows = await connection.execute_dicts(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", [table]
        )
        triggers: list[Trigger] = []
        unparsed_triggers: list[tuple[str, str]] = []
        for row in trigger_rows:
            if row["name"] in index_trigger_names or row["name"].lower() in index_trigger_names:
                continue
            trigger = SqliteIntrospector._parse_sqlite_trigger_def(row["name"], row["sql"])
            if trigger is not None:
                triggers.append(trigger)
            else:
                unparsed_triggers.append((row["name"], row["sql"]))
        return triggers, unparsed_triggers

    @staticmethod
    def _get_table_options(connection: DatabaseClient, table_sql: str) -> TableOptions | None:
        """Reads the ``WITHOUT ROWID``/``STRICT`` options of a table from its CREATE TABLE text, as
        the connection's dialect declares them.

        Args:
            connection: The connection the table was read on.
            table_sql: The table's CREATE TABLE text from ``sqlite_master``.

        Returns:
            The dialect's table options, None when the table has none or the dialect declares none.
        """
        table_options_class = connection.dialect.table_options_class
        if table_options_class is None:
            return None
        options_match = SQLITE_TABLE_OPTIONS_PATTERN.search(table_sql)
        option_words = (
            {" ".join(option.upper().split()) for option in options_match["options"].split(",")}
            if options_match is not None
            else set()
        )
        return table_options_class.from_observed(
            {"without_rowid": "WITHOUT ROWID" in option_words, "strict": "STRICT" in option_words}
        )

    @staticmethod
    def _get_sqlite_key_details(index_terms: list[str]) -> tuple[list[str], list[str] | None, list[str]]:
        """Reads the sort order and collation of each key of an index over plain columns.

        Args:
            index_terms: Each key's text from the CREATE INDEX statement.

        Returns:
            Each key's ``Order`` value (a descending key's NULLs first, as the declaration's), the
            keys without their sort order when one is collated (the index is then an expression
            index) or None, and the keys a model can't declare (a collated key with a sort order).
        """
        key_orders: list[str] = []
        unordered_terms: list[str] = []
        has_collation = False
        unrepresentable_properties: list[str] = []
        for term in index_terms:
            order_match = SQLITE_INDEX_TERM_ORDER_RE.search(term)
            is_descending = order_match is not None and order_match.group("direction").upper() == "DESC"
            key_orders.append((Order.DESC_NULLS_FIRST if is_descending else Order.ASC_NULLS_LAST).value)
            unordered_term = term[: order_match.start()].strip() if order_match else term
            unordered_terms.append(unordered_term)
            if SQLITE_INDEX_TERM_COLLATE_RE.search(unordered_term):
                has_collation = True
                if is_descending:
                    unrepresentable_properties.append(term)
        if not has_collation or unrepresentable_properties:
            return key_orders, None, unrepresentable_properties
        return key_orders, unordered_terms, []

    @staticmethod
    def _parse_sqlite_unique_constraint_names(table_sql: str) -> dict[tuple[str, ...], list[str]]:
        """Reads the names of the table-level UNIQUE constraints out of SQLite's CREATE TABLE text.

        Args:
            table_sql: The table's literal CREATE TABLE statement.

        Returns:
            Column names tuple -> the names of the constraints over exactly those columns, in
            declaration order.
        """
        names_by_columns: dict[tuple[str, ...], list[str]] = {}
        for match in SQLITE_TABLE_UNIQUE_CONSTRAINT_RE.finditer(table_sql or ""):
            column_names = tuple(
                SqliteIntrospector._unquote_sqlite_identifier(column.strip())
                for column in match.group("columns").split(",")
            )
            names_by_columns.setdefault(column_names, []).append(
                SqliteIntrospector._unquote_sqlite_identifier(match.group("name"))
            )
        return names_by_columns

    @staticmethod
    def _unquote_sqlite_identifier(identifier: str) -> str:
        """Strips SQLite's identifier quoting - double quotes or backticks, un-doubling an embedded
        quote character, or square brackets."""
        if len(identifier) >= 2 and identifier[0] == identifier[-1] and identifier[0] in '"`':
            quote = identifier[0]
            return identifier[1:-1].replace(quote * 2, quote)
        if len(identifier) >= 2 and identifier[0] == "[" and identifier[-1] == "]":
            return identifier[1:-1]
        return identifier

    @staticmethod
    def _parse_sqlite_index_definition(index_sql: str) -> tuple[list[str] | None, str | None]:
        """Splits sqlite_master's literal CREATE INDEX text into its key terms and WHERE predicate.

        Args:
            index_sql: The index's CREATE INDEX statement.

        Returns:
            (stripped key term texts, or None when the key list can't be located; the raw WHERE
            predicate text, or None when the index has no parseable WHERE clause).
        """
        on_match = SQLITE_INDEX_ON_RE.search(index_sql)
        if on_match is None:
            return None, None
        open_paren_index = on_match.end() - 1
        terms_sql = SchemaIntrospector.extract_balanced_parens(index_sql, open_paren_index)
        if terms_sql is None:
            return None, None
        index_terms = [term.strip() for term in SchemaIntrospector.split_top_level_terms(terms_sql)]
        remainder = index_sql[open_paren_index + len(terms_sql) + 2 :]
        where_match = SQLITE_INDEX_WHERE_RE.match(remainder)
        return index_terms, where_match.group("condition") if where_match else None

    @staticmethod
    def _parse_sqlite_trigger_def(name: str, sql: str) -> Trigger | None:
        """Reconstructs a Trigger(...) from sqlite_master's literal CREATE TRIGGER text. Returns
        None (rather than raising) for a definition that doesn't fit the expected shape - the
        caller falls back to surfacing the raw text as a comment instead."""
        match = SQLITE_TRIGGERDEF_RE.match(sql.strip())
        if not match:
            return None
        when = match.group("when")
        return Trigger(
            name=name,
            on=SchemaIntrospector.normalize_event_clause(match.group("on")),
            body=RawSQLTerm(match.group("body").strip()),
            # The regex only ever captures BEFORE/AFTER/INSTEAD OF, so this can't fail.
            timing=TriggerTiming(match.group("timing").upper()),
            for_each=TriggerForEach.ROW,
            when=RawSQLTerm(when.strip()) if when else None,
        )

    @staticmethod
    def _sqlite_column_name_alternation(column_name: str) -> str:
        """A regex matching a column's name bare or in any quoting SQLite accepts (double quotes,
        backticks, square brackets).
        """
        escaped = re.escape(column_name)
        return (
            rf'"{re.escape(column_name.replace('"', '""'))}"|`{re.escape(column_name.replace("`", "``"))}`'
            rf"|\[{escaped}\]|\b{escaped}\b"
        )

    @staticmethod
    def _parse_sqlite_generated_column_expression(table_sql: str, column_name: str) -> str | None:
        """Extracts a generated column's expression out of the table's CREATE TABLE text: the column's
        name, its type, an optional ``GENERATED ALWAYS``, then ``AS (expr)``.

        Returns:
            The expression, None when the text has another shape.
        """
        header_re = re.compile(
            rf"(?:{SqliteIntrospector._sqlite_column_name_alternation(column_name)})\s+"
            r"(?:[\w]+(?:\([^()]*\))?\s+)*(?:GENERATED\s+ALWAYS\s+)?AS\s*\(",
            re.IGNORECASE,
        )
        match = header_re.search(table_sql)
        if not match:
            return None
        expression = SchemaIntrospector.extract_balanced_parens(table_sql, match.end() - 1)
        return expression.strip() if expression is not None else None

    @staticmethod
    def _parse_sqlite_check_constraints(
        table: str, table_sql: str
    ) -> tuple[list[CheckConstraint], list[tuple[str, str]]]:
        """Extracts every CHECK constraint - table-level and column-level - out of the table's CREATE
        TABLE text. An unnamed one is named ``<table>_check_<n>``.

        Returns:
            The parsed constraints, and ``(name, matched text)`` of each CHECK whose parentheses
            never balanced.
        """
        constraints: list[CheckConstraint] = []
        unparsed: list[tuple[str, str]] = []
        for ordinal, match in enumerate(SQLITE_CHECK_CONSTRAINT_HEADER_RE.finditer(table_sql), start=1):
            raw_name = match.group("name")
            name = SqliteIntrospector._unquote_sqlite_identifier(raw_name) if raw_name else f"{table}_check_{ordinal}"
            expression = SchemaIntrospector.extract_balanced_parens(table_sql, match.end() - 1)
            if expression is None:
                unparsed.append((name, match.group(0)))
                continue
            constraints.append(CheckConstraint(check=RawSQLTerm(expression.strip()), name=name))
        return constraints, unparsed
