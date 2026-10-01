from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.enums import TriggerForEach, TriggerTiming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.triggers import Trigger
from hare.dialects.sqlite.constants import (
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_CHECK_CONSTRAINT_HEADER_RE,
    SQLITE_DEFAULT_TYPE_AFFINITY,
    SQLITE_EARLIER_COLUMN_TYPES,
    SQLITE_EMPTY_TYPE_AFFINITY,
    SQLITE_INDEX_ON_RE,
    SQLITE_INDEX_TERM_COLLATE_RE,
    SQLITE_INDEX_TERM_MODIFIER_RE,
    SQLITE_INDEX_TERM_ORDER_RE,
    SQLITE_INDEX_WHERE_RE,
    SQLITE_MAIN_SCHEMA,
    SQLITE_NOW_UTC_SQL,
    SQLITE_SIZE_RE,
    SQLITE_TABLE_EXISTS_SQL,
    SQLITE_TABLE_OPTIONS_PATTERN,
    SQLITE_TABLE_UNIQUE_CONSTRAINT_RE,
    SQLITE_TRIGGERDEF_RE,
    SQLITE_TYPE_AFFINITY_RULES,
    SQLITE_TYPE_MAP,
)
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import OnDelete
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


class SqliteIntrospector(SchemaIntrospector):
    """Reads a SQLite schema from ``sqlite_master`` and the ``PRAGMA`` table functions - the
    declared ``CREATE`` text is where SQLite keeps what its pragmas don't report (constraint
    names, generated column expressions, index conditions)."""

    TYPE_MAP = SQLITE_TYPE_MAP
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
        # One schema, no partitions.
        rows = await connection.execute_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row["name"] for row in rows]

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
            return "hare.fields.data.uuids.UUIDField", {}, False
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
        pk_column_names = {row["name"] for row in column_rows if row["pk"]}
        index_rows = await connection.execute_dicts(f"PRAGMA index_list({SchemaIntrospector.quote_identifier(table)})")

        # The CREATE TABLE text as written - the only source of a generated column's expression and
        # of CHECK constraints.
        table_sql_rows = await connection.execute_dicts(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", [table]
        )
        table_sql = table_sql_rows[0]["sql"] if table_sql_rows else ""

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
            if set(index_columns) == pk_column_names:
                continue  # backing the primary key itself, already covered by ColumnInfo.is_pk
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

        columns = []
        unparsed_generated_columns: list[tuple[str, str]] = []
        for row in column_rows:
            length, scale = SqliteIntrospector._parse_sqlite_type_size(row["type"])
            is_decimal_like = "numeric" in row["type"].lower() or "decimal" in row["type"].lower()
            # A generated column's expression is parsed out of the CREATE TABLE text; when it can't
            # be, the column is left out and listed as unparsed rather than kept as an ordinary
            # field.
            is_generated = row["hidden"] in (2, 3)
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

        # foreign_key_list's "id" groups the rows of one constraint, "seq" orders its columns. A
        # composite one becomes one ForeignKeyField when its columns follow the shadow column
        # naming, otherwise its columns stay plain fields and it is listed as unparsed.
        fk_rows = await connection.execute_dicts(
            f"PRAGMA foreign_key_list({SchemaIntrospector.quote_identifier(table)})"
        )
        fk_rows_by_id: dict[int, list[dict[str, Any]]] = {}
        for row in fk_rows:
            fk_rows_by_id.setdefault(row["id"], []).append(row)

        foreign_keys: dict[str, ForeignKeyInfo] = {}
        composite_foreign_keys: list[CompositeForeignKeyInfo] = []
        unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = []
        for fk_id, rows in fk_rows_by_id.items():
            # ForeignKeyFieldInstance already defaults to the target's PK, so to_field= is only
            # needed when the real target column ("to") is something else, e.g. a UNIQUE column.
            target_pk_rows = await connection.execute_dicts(
                f"PRAGMA table_xinfo({SchemaIntrospector.quote_identifier(rows[0]['table'])})"
            )
            target_pk_columns = [
                target_row["name"] for target_row in sorted(target_pk_rows, key=lambda r: r["pk"]) if target_row["pk"]
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
                    SchemaIntrospector._match_composite_foreign_key_naming(
                        list(member_columns), target_columns, target_pk_columns
                    )
                    if len(target_columns) == len(member_columns)
                    else None
                )
                if match is None:
                    unparsed_foreign_keys.append((f"{table}_fk_{fk_id}", member_columns))
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
        # sqlite_master.sql stores the trigger's original, literal CREATE TRIGGER text exactly as
        # written (SQLite has no equivalent of Postgres's pg_get_triggerdef() to canonicalize it
        # first), so _parse_sqlite_trigger_def() has to tolerate more formatting variance.
        trigger_rows = await connection.execute_dicts(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", [table]
        )
        triggers: list[Trigger] = []
        unparsed_triggers: list[tuple[str, str]] = []
        for row in trigger_rows:
            trigger = SqliteIntrospector._parse_sqlite_trigger_def(row["name"], row["sql"])
            if trigger is not None:
                triggers.append(trigger)
            else:
                unparsed_triggers.append((row["name"], row["sql"]))

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
        terms_sql = SchemaIntrospector._extract_balanced_parens(index_sql, open_paren_index)
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
            on=SchemaIntrospector._normalize_event_clause(match.group("on")),
            body=match.group("body").strip(),
            # The regex only ever captures BEFORE/AFTER/INSTEAD OF, so this can't fail.
            timing=TriggerTiming(match.group("timing").upper()),
            for_each=TriggerForEach.ROW,
            when=when.strip() if when else None,
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
        expression = SchemaIntrospector._extract_balanced_parens(table_sql, match.end() - 1)
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
            expression = SchemaIntrospector._extract_balanced_parens(table_sql, match.end() - 1)
            if expression is None:
                unparsed.append((name, match.group(0)))
                continue
            constraints.append(CheckConstraint(check=RawSQLTerm(expression.strip()), name=name))
        return constraints, unparsed
