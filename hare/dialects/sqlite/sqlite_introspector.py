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
    SPATIALITE_GEOMETRY_COLUMN_NOTE,
    SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME,
    SPATIALITE_INDEX_TYPE,
    SPATIALITE_INDEXED_COLUMNS_SQL,
    SPATIALITE_METADATA_MARKER_TABLE_NAMES,
    SPATIALITE_METADATA_TABLE_NAMES,
    SPATIALITE_METADATA_VIEW_NAMES,
    SPATIALITE_TRIGGER_NAME_PREFIXES,
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_CHECK_CONSTRAINT_HEADER_RE,
    SQLITE_COLUMN_TYPE_SYNONYMS,
    SQLITE_DATABASE_LIST_SQL,
    SQLITE_DATABASE_PRAGMA_TEMPLATE,
    SQLITE_DATABASE_TABLE_EXISTS_SQL,
    SQLITE_DEFAULT_TYPE_AFFINITY,
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
    SQLITE_IDENTIFIER_PATTERN,
    SQLITE_INDEX_ON_RE,
    SQLITE_INDEX_SQLS_SQL,
    SQLITE_INDEX_TERM_COLLATE_RE,
    SQLITE_INDEX_TERM_MODIFIER_RE,
    SQLITE_INDEX_TERM_ORDER_RE,
    SQLITE_INDEX_WHERE_RE,
    SQLITE_INT_TYPE,
    SQLITE_MAIN_SCHEMA,
    SQLITE_NOW_UTC_SQL,
    SQLITE_PRIMARY_KEY_INDEX_ORIGIN,
    SQLITE_ROW_ID_TYPE,
    SQLITE_SHARED_COLUMN_TYPE_NOTES,
    SQLITE_SIZE_RE,
    SQLITE_TABLE_EXISTS_SQL,
    SQLITE_TABLE_NAMES_SQL,
    SQLITE_TABLE_OPTIONS_PATTERN,
    SQLITE_TABLE_SQL_SQL,
    SQLITE_TABLE_UNIQUE_CONSTRAINT_RE,
    SQLITE_TRIGGER_SQLS_SQL,
    SQLITE_TRIGGERDEF_RE,
    SQLITE_TYPE_AFFINITY_RULES,
    SQLITE_TYPE_MAP,
    SQLITE_VIEWS_SQL,
    SQLITE_VIRTUAL_TABLES_SQL,
    SQLITE_WITHOUT_ROWID_PATTERN,
)
from hare.dialects.sqlite.indexes.full_text_index import FullTextIndex
from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.dialects.sqlite.schema.constants import SQLITE_COMMENT_UNESCAPES
from hare.dialects.sqlite.spatial.constants import SPATIALITE_COLUMN_TYPES
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import OnDelete
from hare.gis.constants import GEOMETRY_FIELD_CLASS_NAMES
from hare.inspectdb.constants import AMBIGUOUS_REASON_SENTINEL_KWARG
from hare.inspectdb.exceptions import SchemaNotFoundError
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.sql.order import Order

if TYPE_CHECKING:
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient


class SqliteIntrospector(SchemaIntrospector):
    """Reads a SQLite schema from ``sqlite_master`` and the ``PRAGMA`` table functions - the
    declared ``CREATE`` text is where SQLite keeps what its pragmas don't report (constraint
    names, generated column expressions, index conditions). A schema is a database of the connection -
    ``main``, or one it attaches."""

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

    @staticmethod
    async def get_database(connection: DatabaseClient, schema: str) -> str:
        """The database of the connection a schema names, quoted for a statement.

        Args:
            connection: The connection.
            schema: ``main``, or a database the connection attaches.

        Returns:
            The quoted name.

        Raises:
            SchemaNotFoundError: The connection has no such database.
        """
        if schema != SQLITE_MAIN_SCHEMA:
            database_names = {row["name"] for row in await connection.execute_dicts(SQLITE_DATABASE_LIST_SQL)}
            if schema not in database_names:
                raise SchemaNotFoundError(schema)
        return SchemaIntrospector.quote_identifier(schema)

    @staticmethod
    def get_pragma_sql(database: str, pragma: str, name: str) -> str:
        """A table-reading PRAGMA of a database of the connection.

        Args:
            database: The quoted database.
            pragma: ``table_xinfo``, ``index_list``, ``index_info`` or ``foreign_key_list``.
            name: The table or index, as ``sqlite_master`` names it.

        Returns:
            The statement.
        """
        return SQLITE_DATABASE_PRAGMA_TEMPLATE.format(
            database=database, pragma=pragma, name=SchemaIntrospector.quote_identifier(name)
        )

    @classmethod
    async def fetch_views(cls, connection: DatabaseClient, schema: str) -> list[tuple[str, bool, tuple[str, ...]]]:
        database = await SqliteIntrospector.get_database(connection, schema)
        rows = await connection.execute_dicts(SQLITE_VIEWS_SQL.format(database=database))
        views = [
            (row["name"], False, tuple(sorted(row["trigger_names"].split(","))) if row["trigger_names"] else ())
            for row in rows
        ]
        # The views of SpatiaLite's spatial metadata are no model's, as its tables aren't.
        if await SqliteIntrospector.has_spatial_metadata(connection, database):
            views = [view for view in views if view[0] not in SPATIALITE_METADATA_VIEW_NAMES]
        return views

    @staticmethod
    async def has_spatial_metadata(connection: DatabaseClient, database: str) -> bool:
        """Whether a database has SpatiaLite's spatial metadata - every table it always has.

        Args:
            connection: The connection.
            database: The database, as SQLite names it (``main``, an attached one).

        Returns:
            True when it has the metadata.
        """
        rows = await connection.execute_dicts(SQLITE_TABLE_NAMES_SQL.format(database=database))
        return {row["name"] for row in rows} >= SPATIALITE_METADATA_MARKER_TABLE_NAMES

    @classmethod
    async def fetch_table_names(cls, connection: DatabaseClient, schema: str, include_partitions: bool) -> list[str]:
        # No partitions. A virtual table - a FullTextIndex's FTS5 table among them - and the shadow
        # tables keeping its data, named after it, are no model's.
        database = await SqliteIntrospector.get_database(connection, schema)
        rows = await connection.execute_dicts(SQLITE_TABLE_NAMES_SQL.format(database=database))
        virtual_table_names = [
            row["name"] for row in await connection.execute_dicts(SQLITE_VIRTUAL_TABLES_SQL.format(database=database))
        ]
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
    async def fetch_full_text_indexes(connection: DatabaseClient, database: str, table: str) -> list[IndexInfo]:
        """The ``FullTextIndex``es of a table - the FTS5 tables reading it.

        Args:
            connection: The connection.
            database: The quoted database of the table.
            table: The table.

        Returns:
            The indexes.
        """
        full_text_indexes = []
        for row in await connection.execute_dicts(SQLITE_VIRTUAL_TABLES_SQL.format(database=database)):
            index_info = SqliteIntrospector.get_full_text_index_info(table, row["name"], row["sql"])
            if index_info is not None:
                full_text_indexes.append(index_info)
        return full_text_indexes

    @staticmethod
    async def fetch_spatial_indexes(
        connection: DatabaseClient, database: str, table: str, column_names: Sequence[str]
    ) -> list[IndexInfo]:
        """The ``SpatialiteIndex``es of a table - its columns SpatiaLite's spatial metadata has an
        enabled spatial index of. The metadata keeps names lowercased; each is read back as the
        table names its column.

        Args:
            connection: The connection.
            database: The quoted database of the table - the one holding its spatial metadata.
            table: The table.
            column_names: The table's columns.

        Returns:
            The indexes - unnamed: SpatiaLite names their tables.
        """
        _, metadata_rows = await connection.execute(
            SQLITE_DATABASE_TABLE_EXISTS_SQL.format(database=database), [SPATIALITE_GEOMETRY_COLUMNS_TABLE_NAME]
        )
        if not metadata_rows:
            return []
        column_names_by_lowercase_name = {column_name.lower(): column_name for column_name in column_names}
        spatial_indexes = []
        for row in await connection.execute_dicts(SPATIALITE_INDEXED_COLUMNS_SQL.format(database=database), [table]):
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
        database = await SqliteIntrospector.get_database(connection, schema)
        return [
            await SqliteIntrospector._inspect_table_sqlite(connection, schema, database, table) for table in tables
        ]

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
        affinity, whatever else the declared type spells.

        Args:
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            Whether they differ.
        """
        return cls.get_type_affinity(declared_type) != cls.get_type_affinity(column.db_type)

    @classmethod
    def map_column_type(cls, column: ColumnInfo) -> tuple[str, dict[str, Any], bool]:
        if column.db_type.lower().startswith("char") and column.max_length == 36:
            # No native UUID type - UUIDField declares one as plain "CHAR(36)", indistinguishable
            # from a genuine CharField(max_length=36) at the schema level.
            return "hare.fields.data.uuid_field.UUIDField", {}, False
        spatial_mapping = cls.get_spatial_field_mapping(column)
        if spatial_mapping is not None:
            return spatial_mapping
        path, kwargs, is_ambiguous = super().map_column_type(column)
        shared_type_note = SQLITE_SHARED_COLUMN_TYPE_NOTES.get(column.db_type.lower().replace(" ", ""))
        if shared_type_note is not None and not column.is_pk:
            kwargs[AMBIGUOUS_REASON_SENTINEL_KWARG] = shared_type_note
        return path, kwargs, is_ambiguous

    @staticmethod
    def get_spatial_field_mapping(column: ColumnInfo) -> tuple[str, dict[str, Any], bool] | None:
        """The field of a column of a geometry type hare writes on SQLite - ``POINT``, ``POINTZ``: the
        ``GeometryField`` of that type, its SRID and geography left to be checked. SQLite would read
        ``POINT`` as an integer column - its name holds ``INT``.

        Args:
            column: The column.

        Returns:
            (field_path, extra_kwargs, is_ambiguous), None for a column of another type.
        """
        column_type = column.db_type.strip().upper()
        dimensions = 2
        geometry_type = next(
            (geometry_type for geometry_type, name in SPATIALITE_COLUMN_TYPES.items() if name == column_type), None
        )
        if geometry_type is None and column_type.endswith("Z"):
            dimensions = 3
            geometry_type = next(
                (
                    geometry_type
                    for geometry_type, name in SPATIALITE_COLUMN_TYPES.items()
                    if f"{name}Z" == column_type
                ),
                None,
            )
        if geometry_type is None:
            return None
        field_kwargs: dict[str, Any] = {AMBIGUOUS_REASON_SENTINEL_KWARG: SPATIALITE_GEOMETRY_COLUMN_NOTE}
        if dimensions == 3:
            field_kwargs["dimensions"] = 3
        return f"hare.gis.fields.{GEOMETRY_FIELD_CLASS_NAMES[geometry_type]}", field_kwargs, False

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
    async def _inspect_table_sqlite(connection: DatabaseClient, schema: str, database: str, table: str) -> TableInfo:
        # table_xinfo, not table_info, which doesn't list generated columns. hidden: 0 normal, 1 a
        # virtual table's hidden column (skipped), 2 VIRTUAL generated, 3 STORED generated.
        raw_column_rows = await connection.execute_dicts(
            SqliteIntrospector.get_pragma_sql(database, "table_xinfo", table)
        )
        column_rows = [row for row in raw_column_rows if row["hidden"] != 1]

        # The CREATE TABLE text as written - the only source of a generated column's expression and
        # of CHECK constraints.
        table_sql_rows = await connection.execute_dicts(SQLITE_TABLE_SQL_SQL.format(database=database), [table])
        table_sql = table_sql_rows[0]["sql"] if table_sql_rows else ""

        (
            indexes,
            column_indexes,
            unparsed_indexes,
            unique_columns,
            indexed_columns,
        ) = await SqliteIntrospector._read_sqlite_indexes(connection, database, table, table_sql)
        columns, unparsed_generated_columns = SqliteIntrospector._read_sqlite_columns(
            column_rows, table_sql, unique_columns, indexed_columns
        )
        table_description, column_descriptions = SqliteIntrospector.get_comments(table_sql)
        for column in columns:
            column.description = column_descriptions.get(column.name)
        (
            foreign_keys,
            composite_foreign_keys,
            unparsed_foreign_keys,
        ) = await SqliteIntrospector._read_sqlite_foreign_keys(connection, database, table)
        extension_indexes, index_trigger_names = await SqliteIntrospector._read_sqlite_extension_indexes(
            connection, database, table, [row["name"] for row in column_rows]
        )
        indexes.extend(extension_indexes)
        triggers, unparsed_triggers = await SqliteIntrospector._read_sqlite_triggers(
            connection, database, table, index_trigger_names
        )

        check_constraints, unparsed_check_constraints = SqliteIntrospector._parse_sqlite_check_constraints(
            table, table_sql
        )

        return TableInfo(
            name=table,
            schema=schema,
            is_in_default_schema=schema == SQLITE_MAIN_SCHEMA,
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
            table_description=table_description,
        )

    @staticmethod
    async def _read_sqlite_indexes(
        connection: DatabaseClient, database: str, table: str, table_sql: str
    ) -> tuple[list[IndexInfo], list[IndexInfo], list[tuple[str, str]], set[str], set[str]]:
        """Reads a table's indexes - each an index of its own, a single column's (``index=True``,
        ``unique=True``) or one hare can't rebuild.

        Args:
            connection: The connection.
            database: The quoted database of the table.
            table: The table.
            table_sql: The table's CREATE TABLE text.

        Returns:
            The indexes, the single columns' indexes, the unparsed indexes' names and SQL, and the
            unique and the indexed single columns.
        """
        # By name: PRAGMA index_list lists them in the order they were made, which a migrate of the model read
        # back makes in another order - its model would come back otherwise.
        index_rows = sorted(
            await connection.execute_dicts(SqliteIntrospector.get_pragma_sql(database, "index_list", table)),
            key=lambda row: row["name"],
        )

        # sqlite_master.sql stores each index's original, literal CREATE INDEX text - the only
        # source of an expression term's text and of a partial index's WHERE predicate.
        index_sql_rows = await connection.execute_dicts(SQLITE_INDEX_SQLS_SQL.format(database=database), [table])
        index_sql_by_name = {row["name"]: row["sql"] for row in index_sql_rows}

        unique_columns: set[str] = set()
        indexed_columns: set[str] = set()
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        unparsed_indexes: list[tuple[str, str]] = []
        unique_constraint_names_by_columns = SqliteIntrospector._parse_sqlite_unique_constraint_names(table_sql)
        for index_row in index_rows:
            index_column_rows = await connection.execute_dicts(
                SqliteIntrospector.get_pragma_sql(database, "index_info", index_row["name"])
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
                # literal CREATE INDEX text, unless a term carries a collation; each term's sort order
                # is read apart.
                expression_key_orders: list[str] = []
                if index_terms is not None and not any(
                    SQLITE_INDEX_TERM_COLLATE_RE.search(term) for term in index_terms
                ):
                    expression_key_orders = SqliteIntrospector._get_sqlite_key_details(index_terms)[0]
                    # The parentheses hare writes around an expression key are no part of it.
                    index_terms = [
                        SchemaIntrospector.strip_outer_parentheses(SQLITE_INDEX_TERM_ORDER_RE.sub("", term)) or ""
                        for term in index_terms
                    ]
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
                        key_orders=expression_key_orders,
                        condition_sql=SchemaIntrospector.strip_outer_parentheses(raw_condition),
                    )
                )
                continue
            key_orders, collated_terms, unrepresentable_properties = SqliteIntrospector._get_sqlite_key_details(
                # The index of a UNIQUE constraint has no text - each key in its default order.
                index_terms if index_terms is not None else [""] * len(index_columns)
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
        # The one key column declared INTEGER of a rowid table is the row id, which SQLite fills in.
        has_row_id_key = sum(1 for row in column_rows if row["pk"]) == 1 and not SQLITE_WITHOUT_ROWID_PATTERN.search(
            table_sql or ""
        )
        for row in column_rows:
            length, scale = SqliteIntrospector._parse_sqlite_type_size(row["type"])
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
                    # INTEGER is the type of the row id alone - the one key column of a rowid table; elsewhere
                    # it is INT, which an IntField declares. Upper-cased - SQLite before 3.37 gives the type as
                    # it was written (int), a later one upper-cased, as hare writes it; a synonym of a type
                    # hare declares (DATETIME) read as hare's own.
                    db_type=SQLITE_INT_TYPE
                    if not (has_row_id_key and row["pk"]) and row["type"].strip().upper() == SQLITE_ROW_ID_TYPE
                    else SQLITE_COLUMN_TYPE_SYNONYMS.get(row["type"].strip().upper(), row["type"].upper()),
                    # A primary key column declared without NOT NULL takes NULL on SQLite, which no
                    # key field holds - read as NOT NULL.
                    nullable=not row["notnull"] and not row["pk"],
                    is_pk=bool(row["pk"]),
                    # PRAGMA table_xinfo's own "pk" value IS already the 1-based declared
                    # position within the PK (0 for a non-PK column) - see ColumnInfo.pk_position.
                    pk_position=row["pk"] or None,
                    is_unique=row["name"] in unique_columns,
                    has_index=row["name"] in indexed_columns,
                    max_length=length,
                    db_default=(
                        SqliteIntrospector._parse_sqlite_db_default(row["dflt_value"])
                        if row["dflt_value"] is not None
                        else None
                    ),
                    generated_expression=generated_expression,
                    generated_stored=row["hidden"] == 3,
                    generates_key=has_row_id_key
                    and bool(row["pk"])
                    and row["type"].strip().upper() == SQLITE_ROW_ID_TYPE,
                )
            )

        return columns, unparsed_generated_columns

    @staticmethod
    async def _read_sqlite_foreign_keys(
        connection: DatabaseClient, database: str, table: str
    ) -> tuple[dict[str, ForeignKeyInfo], list[CompositeForeignKeyInfo], list[tuple[str, tuple[str, ...]]]]:
        """Reads a table's foreign keys - each to a table of the same database.

        Args:
            connection: The connection.
            database: The quoted database of the table.
            table: The table.

        Returns:
            The single-column foreign keys by column, the composite ones, and the names and columns
            of the composite ones not following the shadow column naming.
        """
        # foreign_key_list's "id" groups the rows of one constraint, "seq" orders its columns. A
        # composite one becomes one ForeignKeyField when its columns follow the shadow column
        # naming, otherwise its columns stay plain fields and it is listed as unparsed.
        foreign_key_rows = await connection.execute_dicts(
            SqliteIntrospector.get_pragma_sql(database, "foreign_key_list", table)
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
                SqliteIntrospector.get_pragma_sql(database, "table_xinfo", rows[0]["table"])
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
                        on_update=ordered_rows[0]["on_update"],
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
                on_update=row["on_update"],
            )
        return foreign_keys, composite_foreign_keys, unparsed_foreign_keys

    @staticmethod
    async def _read_sqlite_extension_indexes(
        connection: DatabaseClient, database: str, table: str, column_names: list[str]
    ) -> tuple[list[IndexInfo], set[str]]:
        """Reads the indexes of a table an extension keeps - FTS5's full-text indexes, SpatiaLite's
        spatial ones - with the names of the triggers keeping them, which belong to the indexes.

        Args:
            connection: The connection.
            database: The quoted database of the table.
            table: The table.
            column_names: The table's column names.

        Returns:
            The indexes and the names of their triggers, lower-cased for a spatial index.
        """
        indexes: list[IndexInfo] = []
        # A FullTextIndex is an index of the table, its triggers part of it.
        full_text_indexes = await SqliteIntrospector.fetch_full_text_indexes(connection, database, table)
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
        spatial_indexes = await SqliteIntrospector.fetch_spatial_indexes(connection, database, table, column_names)
        indexes.extend(spatial_indexes)
        index_trigger_names.update(
            f"{prefix}_{table}_{index.columns[0]}".lower()
            for index in spatial_indexes
            for prefix in SPATIALITE_TRIGGER_NAME_PREFIXES
        )
        return indexes, index_trigger_names

    @staticmethod
    async def _read_sqlite_triggers(
        connection: DatabaseClient, database: str, table: str, index_trigger_names: set[str]
    ) -> tuple[list[Trigger], list[tuple[str, str]]]:
        """Reads a table's triggers, those of its extensions' indexes left out.

        Args:
            connection: The connection.
            database: The quoted database of the table.
            table: The table.
            index_trigger_names: The names of the triggers of the table's extension indexes.

        Returns:
            The triggers, and the unparsed triggers' names and SQL.
        """
        # sqlite_master.sql stores the trigger's original, literal CREATE TRIGGER text exactly as
        # written (SQLite has no function to canonicalize it
        # first), so _parse_sqlite_trigger_def() has to tolerate more formatting variance.
        trigger_rows = await connection.execute_dicts(SQLITE_TRIGGER_SQLS_SQL.format(database=database), [table])
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
    def get_comments(table_sql: str) -> tuple[str | None, dict[str, str]]:
        """The comments hare writes in a CREATE TABLE text - the table's first in its column list, each
        column's after its definition - read through the quotes, the parentheses and the comments of the
        text.

        Args:
            table_sql: The table's CREATE TABLE text from ``sqlite_master``.

        Returns:
            The table's comment, and the columns' comments by column name.
        """
        # Each term of the column list: its text without comments, and its comments with whether one
        # came before any of its text.
        terms: list[tuple[list[str], list[tuple[str, bool]]]] = []
        depth = 0
        position = 0
        length = len(table_sql)
        while position < length:
            char = table_sql[position]
            if char in "\"'`[":
                closing = "]" if char == "[" else char
                end = position + 1
                while end < length:
                    if table_sql[end] == closing:
                        # A doubled quote is one quote of the text.
                        if closing != "]" and end + 1 < length and table_sql[end + 1] == closing:
                            end += 2
                            continue
                        break
                    end += 1
                if depth > 0:
                    terms[-1][0].append(table_sql[position : end + 1])
                position = end + 1
                continue
            if table_sql.startswith("/*", position):
                end = table_sql.find("*/", position + 2)
                end = length if end == -1 else end
                if depth > 0:
                    text, comments = terms[-1]
                    comments.append((table_sql[position + 2 : end].strip(), not "".join(text).strip()))
                position = end + 2
                continue
            if table_sql.startswith("--", position):
                end = table_sql.find("\n", position)
                position = length if end == -1 else end
                continue
            if char == "(":
                depth += 1
                if depth == 1:
                    terms.append(([], []))
                    position += 1
                    continue
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            elif char == "," and depth == 1:
                terms.append(([], []))
                position += 1
                continue
            if depth > 0:
                terms[-1][0].append(char)
            position += 1
        table_description = None
        column_descriptions: dict[str, str] = {}
        for index, (text, comments) in enumerate(terms):
            if index == 0 and comments and comments[0][1]:
                table_description = SqliteIntrospector.unescape_comment(comments[0][0])
                comments = comments[1:]
            name_match = re.match(rf"\s*({SQLITE_IDENTIFIER_PATTERN})", "".join(text))
            if comments and name_match is not None:
                name = SqliteIntrospector._unquote_sqlite_identifier(name_match[1])
                column_descriptions[name] = SqliteIntrospector.unescape_comment(comments[-1][0])
        return table_description, column_descriptions

    @staticmethod
    def unescape_comment(comment: str) -> str:
        """A comment's text without the escapes hare writes it with.

        Args:
            comment: The text between ``/*`` and ``*/``, stripped.

        Returns:
            The text.
        """
        return re.sub(r"\\(.)", lambda match: SQLITE_COMMENT_UNESCAPES.get(match[1], match[0]), comment)

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
            index) or None, and the keys a model can't declare (a collated descending key).
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
        # A condition written with or without parentheses around it - (a) OR (b) keeps its own.
        when = SchemaIntrospector.strip_outer_parentheses(match.group("when"))
        return Trigger(
            name=name,
            on=SchemaIntrospector.normalize_event_clause(match.group("on")),
            body=RawSQLTerm(match.group("body").strip()),
            # The regex only ever captures BEFORE/AFTER/INSTEAD OF, so this can't fail.
            timing=TriggerTiming(match.group("timing").upper()),
            for_each=TriggerForEach.ROW,
            when=RawSQLTerm(when) if when else None,
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
