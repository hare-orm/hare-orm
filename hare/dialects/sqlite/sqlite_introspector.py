from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from sqlparse import tokens
from sqlparse.lexer import tokenize

from hare.ddl.enums import TriggerForEach, TriggerTiming
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.sqlite.constants import (
    SPATIALITE_GEOMETRY_COLUMN_NOTE,
    SPATIALITE_INDEX_TYPE,
    SPATIALITE_METADATA_MARKER_TABLE_NAMES,
    SPATIALITE_METADATA_TABLE_NAMES,
    SPATIALITE_METADATA_VIEW_NAMES,
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_COLUMN_TYPE_SYNONYMS,
    SQLITE_DATABASE_LIST_SQL,
    SQLITE_DATABASE_PRAGMA_TEMPLATE,
    SQLITE_FULL_TEXT_ARGUMENT_PATTERN,
    SQLITE_FULL_TEXT_CONTENT_OPTION,
    SQLITE_FULL_TEXT_CONTENT_ROWID_OPTION,
    SQLITE_FULL_TEXT_MODULE,
    SQLITE_FULL_TEXT_TABLE_PATTERN,
    SQLITE_FULL_TEXT_TOKENIZE_OPTION,
    SQLITE_IDENTIFIER_PATTERN,
    SQLITE_INDEX_ON_RE,
    SQLITE_INDEX_TERM_COLLATE_RE,
    SQLITE_INDEX_TERM_ORDER_RE,
    SQLITE_INDEX_WHERE_RE,
    SQLITE_INT_TYPE,
    SQLITE_MAIN_SCHEMA,
    SQLITE_NOW_UTC_SQL,
    SQLITE_ROW_ID_TYPE,
    SQLITE_SHARED_COLUMN_TYPE_NOTES,
    SQLITE_SIZE_RE,
    SQLITE_TABLE_EXISTS_SQL,
    SQLITE_TABLE_NAMES_SQL,
    SQLITE_TABLE_OPTIONS_PATTERN,
    SQLITE_TRIGGERDEF_RE,
    SQLITE_TYPE_MAP,
    SQLITE_VIEWS_SQL,
    SQLITE_VIRTUAL_TABLES_SQL,
)
from hare.dialects.sqlite.indexes.full_text_index import FullTextIndex
from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.dialects.sqlite.introspection.sqlite_catalog_parts import SqliteCatalogParts
from hare.dialects.sqlite.introspection.sqlite_catalog_reader import SqliteCatalogReader
from hare.dialects.sqlite.introspection.sqlite_column_declarations import SqliteColumnDeclarations
from hare.dialects.sqlite.introspection.sqlite_declaration_mapper import SqliteDeclarationMapper
from hare.dialects.sqlite.schema.constants import SQLITE_COMMENT_UNESCAPES
from hare.dialects.sqlite.spatial.constants import SPATIALITE_COLUMN_TYPES
from hare.dialects.sqlite.types.sqlite_physical_type import SqlitePhysicalType
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.gis.constants import GEOMETRY_FIELD_CLASS_NAMES
from hare.inspectdb.constants import AMBIGUOUS_REASON_SENTINEL_KWARG
from hare.inspectdb.exceptions import SchemaNotFoundError
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.catalog_sql import CatalogSql
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.sql.order import Order

if TYPE_CHECKING:
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.inspectdb.introspection.schema_part import SchemaPart


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
                columns.append(SqliteColumnDeclarations.get_identifier(argument_match["column"]))
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

    @classmethod
    async def fetch_table_exists(cls, connection: DatabaseClient, table: str) -> bool:
        _, rows = await connection.execute(SQLITE_TABLE_EXISTS_SQL, [table])
        return bool(rows)

    @classmethod
    def get_table_infos(cls, snapshot: CatalogSnapshot, dialect: Dialect) -> list[TableInfo]:
        return SqliteDeclarationMapper(cls, dialect.table_options_class).get_tables(snapshot)

    @classmethod
    def get_table_parts(cls, snapshot: CatalogSnapshot, table_infos: list[TableInfo]) -> list[SchemaPart]:
        return SqliteCatalogParts.get(snapshot, table_infos, cls)

    @classmethod
    async def fetch_catalog(cls, connection: DatabaseClient, tables: list[str], schema: str) -> CatalogSnapshot:
        """Capture raw metadata before mapping it to fields and indexes."""
        return await SqliteCatalogReader.read_tables(connection, tables, schema, cls)

    @staticmethod
    def get_type_affinity(type_sql: str) -> str:
        """The type affinity SQLite gives a column declared with ``type_sql``.

        Args:
            type_sql: The declared type.

        Returns:
            INTEGER, TEXT, BLOB, REAL or NUMERIC.
        """
        return SqlitePhysicalType.from_sql(type_sql).affinity

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
    def _parse_sqlite_type_size(db_type: str) -> int | None:
        """Read the first declared type size, or None for an unsized type such as TEXT."""
        match = SQLITE_SIZE_RE.search(db_type)
        return int(match.group(1)) if match else None

    @staticmethod
    def get_referenced_column_type(column: ColumnInfo) -> str:
        """A rowid key's INTEGER becomes INT in an ordinary reference column."""
        if column.generates_key and column.db_type == SQLITE_ROW_ID_TYPE:
            return SQLITE_INT_TYPE
        return column.db_type

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
        # A rendered SqlDefault may already have carried a wrapping pair before
        # SQLite removed its DEFAULT-clause pair. Keep the catalog form stable.
        db_default = SqliteIntrospector.parse_db_default(SchemaIntrospector.strip_outer_parentheses(raw_sql) or "")
        if type(db_default) is SqlDefault:
            return SqlDefault(f"({db_default.sql})")
        return db_default

    @staticmethod
    def _read_sqlite_columns(
        column_rows: list[dict[str, Any]],
        table_sql: str,
        unique_columns: set[str],
        indexed_columns: set[str],
        *,
        has_primary_index: bool,
    ) -> tuple[list[ColumnInfo], list[tuple[str, str]]]:
        """The columns of a table's ``PRAGMA table_xinfo`` rows - a generated column whose expression
        can't be parsed out of the CREATE TABLE text is left out and listed as unparsed.

        Args:
            column_rows: The rows, a virtual table's hidden columns left out.
            table_sql: The table's CREATE TABLE text.
            unique_columns: The columns of a single-column unique index.
            indexed_columns: The columns of a single-column index.
            has_primary_index: Whether the primary key has a separate backing index.

        Returns:
            The columns, and the unparsed generated columns' names with the table's SQL.
        """
        columns = []
        unparsed_generated_columns: list[tuple[str, str]] = []
        column_declarations = SqliteColumnDeclarations.get_columns(table_sql, {row["name"] for row in column_rows})
        # The one key column declared INTEGER of a rowid table is the row id, which SQLite fills in.
        has_row_id_key = sum(1 for row in column_rows if row["pk"]) == 1 and not has_primary_index
        for row in column_rows:
            length = SqliteIntrospector._parse_sqlite_type_size(row["type"])
            # A generated column's expression is parsed out of the CREATE TABLE text; when it can't
            # be, the column is left out and listed as unparsed rather than kept as an ordinary
            # field.
            is_generated = row["hidden"] in {2, 3}
            generated_expression = (
                SqliteColumnDeclarations.get_generated_expression(column_declarations.get(row["name"], ""))
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
                    nullable=not row["notnull"]
                    and not (has_row_id_key and row["pk"] and row["type"].strip().upper() == SQLITE_ROW_ID_TYPE),
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
        for token_type, value in tokenize(table_sql):
            if token_type in tokens.Comment:
                if depth > 0 and value.startswith("/*"):
                    text, comments = terms[-1]
                    comments.append((value[2:-2].strip(), not "".join(text).strip()))
                continue
            if token_type is tokens.Punctuation and value == "(":
                depth += 1
                if depth == 1:
                    terms.append(([], []))
                    continue
            elif token_type is tokens.Punctuation and value == ")":
                depth -= 1
                if depth == 0:
                    break
            elif token_type is tokens.Punctuation and value == "," and depth == 1:
                terms.append(([], []))
                continue
            if depth > 0:
                terms[-1][0].append(value)
        table_description = None
        column_descriptions: dict[str, str] = {}
        for index, (text, comments) in enumerate(terms):
            if index == 0 and comments and comments[0][1]:
                table_description = SqliteIntrospector.unescape_comment(comments[0][0])
                comments = comments[1:]
            name_match = re.match(rf"\s*({SQLITE_IDENTIFIER_PATTERN})", "".join(text))
            if comments and name_match is not None:
                name = SqliteColumnDeclarations.get_identifier(name_match[1])
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
    def _get_table_options(table_options_class: type[TableOptions] | None, table_sql: str) -> TableOptions | None:
        """Reads the ``WITHOUT ROWID``/``STRICT`` options of a table from its CREATE TABLE text, as
        the connection's dialect declares them.

        Args:
            table_options_class: The configured dialect's table option declaration.
            table_sql: The table's CREATE TABLE text from ``sqlite_master``.

        Returns:
            The dialect's table options, None when the table has none or the dialect declares none.
        """
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
            is_descending = order_match is not None and order_match.group("direction").upper() == Order.DESC
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
        terms_sql = CatalogSql.extract_parentheses(index_sql, open_paren_index)
        if terms_sql is None:
            return None, None
        index_terms = [term.strip() for term in CatalogSql.split_terms(terms_sql)]
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
