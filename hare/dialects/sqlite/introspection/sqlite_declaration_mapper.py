from __future__ import annotations

from collections import Counter
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.sqlite.constants import (
    SPATIALITE_INDEX_TYPE,
    SPATIALITE_TRIGGER_NAME_PREFIXES,
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX,
    SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX,
    SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX,
    SQLITE_INDEX_TERM_COLLATE_RE,
    SQLITE_INDEX_TERM_MODIFIER_RE,
    SQLITE_INDEX_TERM_ORDER_RE,
    SQLITE_MAIN_SCHEMA,
    SQLITE_PRIMARY_KEY_INDEX_ORIGIN,
)
from hare.dialects.sqlite.introspection.sqlite_constraint_clauses import SqliteConstraintClauses
from hare.dialects.sqlite.introspection.sqlite_foreign_key_clauses import SqliteForeignKeyClauses
from hare.fields.enums import OnDelete
from hare.inspectdb.introspection.catalog_diagnostic import CatalogDiagnostic
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.enums import CatalogDiagnosticCode
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.sql.order import Order

if TYPE_CHECKING:
    from hare.ddl.table_options import TableOptions
    from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector


class SqliteDeclarationMapper:
    """Infer SQLite declarations from a captured catalog; mapping performs no IO."""

    def __init__(self, introspector: type[SqliteIntrospector], table_options_class: type[TableOptions] | None) -> None:
        self.introspector = introspector
        self.table_options_class = table_options_class

    def get_tables(self, snapshot: CatalogSnapshot) -> list[TableInfo]:
        """Map independent model descriptions in the requested table order."""
        if not snapshot.tables:
            return []
        records = snapshot.copy_records()
        by_table = {row["table_name"]: row for row in records["tables"]}
        target_columns = {row["table_name"]: row["columns"] for row in [*records["tables"], *records["references"]]}
        return [
            self.build_table(by_table[table], snapshot.schema, records["virtual_tables"], target_columns)
            for table in snapshot.tables
        ]

    def build_table(
        self,
        raw: dict[str, Any],
        schema: str,
        virtual_tables: list[dict[str, Any]],
        target_columns: dict[str, list[dict[str, Any]]],
    ) -> TableInfo:
        """Map one table from immutable-source records, with no database access."""
        table = raw["table_name"]
        # table_xinfo, not table_info, which doesn't list generated columns. hidden: 0 normal, 1 a
        # virtual table's hidden column (skipped), 2 VIRTUAL generated, 3 STORED generated.
        raw_column_rows = raw["columns"]
        column_rows = [row for row in raw_column_rows if row["hidden"] != 1]

        # The CREATE TABLE text as written - the only source of a generated column's expression and
        # of CHECK constraints.
        table_sql_rows = raw["table_rows"]
        table_sql = table_sql_rows[0]["sql"] if table_sql_rows else ""

        (
            indexes,
            column_indexes,
            unparsed_indexes,
            unique_columns,
            indexed_columns,
        ) = self.get_indexes(raw, table_sql)
        columns, unparsed_generated_columns = self.introspector._read_sqlite_columns(
            column_rows,
            table_sql,
            unique_columns,
            indexed_columns,
            has_primary_index=any(row["origin"] == SQLITE_PRIMARY_KEY_INDEX_ORIGIN for row in raw["indexes"]),
        )
        table_description, column_descriptions = self.introspector.get_comments(table_sql)
        for column in columns:
            column.description = column_descriptions.get(column.name)
        (
            foreign_keys,
            composite_foreign_keys,
            unparsed_foreign_keys,
        ) = self.get_foreign_keys(
            raw["foreign_keys"], target_columns, table, SqliteForeignKeyClauses.get_deferrals(raw, target_columns)
        )
        extension_indexes, index_trigger_names = self.get_extension_indexes(
            table, [row["name"] for row in column_rows], virtual_tables, raw["spatial_columns"]
        )
        indexes.extend(extension_indexes)
        triggers, unparsed_triggers = self.get_triggers(raw["triggers"], index_trigger_names)

        check_constraints, unparsed_check_constraints = SqliteConstraintClauses.get_checks(table, table_sql)

        return TableInfo(
            name=table,
            diagnostics=[
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.TRIGGER, name, definition)
                    for name, definition in unparsed_triggers
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.FOREIGN_KEY, name, columns=columns)
                    for name, columns in unparsed_foreign_keys
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.INDEX, name, definition)
                    for name, definition in unparsed_indexes
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.GENERATED_COLUMN, name, definition)
                    for name, definition in unparsed_generated_columns
                ),
                *(
                    CatalogDiagnostic(CatalogDiagnosticCode.CHECK_CONSTRAINT, name, definition)
                    for name, definition in unparsed_check_constraints
                ),
            ],
            schema=schema,
            is_in_default_schema=schema == SQLITE_MAIN_SCHEMA,
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            column_indexes=column_indexes,
            triggers=triggers,
            composite_foreign_keys=composite_foreign_keys,
            check_constraints=check_constraints,
            table_options=self.introspector._get_table_options(self.table_options_class, table_sql),
            table_description=table_description,
        )

    def get_indexes(
        self, raw: dict[str, Any], table_sql: str
    ) -> tuple[list[IndexInfo], list[IndexInfo], list[tuple[str, str]], set[str], set[str]]:
        """Infer index declarations from captured pragmas and exact CREATE text."""
        index_rows = sorted(raw["indexes"], key=itemgetter("name"))
        index_sql_by_name = {row["name"]: row["sql"] for row in raw["index_definitions"]}
        unique_columns: set[str] = set()
        indexed_columns: set[str] = set()
        indexes: list[IndexInfo] = []
        column_indexes: list[IndexInfo] = []
        unparsed_indexes: list[tuple[str, str]] = []
        unique_constraint_names_by_columns = SqliteConstraintClauses.get_unique_names(table_sql)
        for index_row in index_rows:
            index_column_rows = [row for row in raw["index_columns"][index_row["name"]] if row["key"]]
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
                self.introspector._parse_sqlite_index_definition(raw_sql) if raw_sql else (None, None)
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
                    expression_key_orders = self.introspector._get_sqlite_key_details(index_terms)[0]
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
            key_orders, collated_terms, unrepresentable_properties = self.introspector._get_sqlite_key_details(
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
                catalog_name=index_row["name"],
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
    def get_foreign_keys(
        foreign_key_rows: list[dict[str, Any]],
        target_columns: dict[str, list[dict[str, Any]]],
        table: str,
        deferrals: dict[int, str | None],
    ) -> tuple[dict[str, ForeignKeyInfo], list[CompositeForeignKeyInfo], list[tuple[str, tuple[str, ...]]]]:
        """Map constraints from catalog records, including keys of external target tables."""
        foreign_key_rows_by_id: dict[int, list[dict[str, Any]]] = {}
        for row in foreign_key_rows:
            foreign_key_rows_by_id.setdefault(row["id"], []).append(row)

        foreign_keys: dict[str, ForeignKeyInfo] = {}
        composite_foreign_keys: list[CompositeForeignKeyInfo] = []
        unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = []
        column_counts = Counter(row["from"] for row in foreign_key_rows)
        for foreign_key_id, rows in foreign_key_rows_by_id.items():
            member_columns = tuple(row["from"] for row in sorted(rows, key=itemgetter("seq")))
            if any(column_counts[column] > 1 for column in member_columns):
                unparsed_foreign_keys.append((f"{table}_fk_{foreign_key_id}", member_columns))
                continue
            # ForeignKeyFieldInstance already defaults to the target's PK, so to_field= is only
            # needed when the real target column ("to") is something else, e.g. a UNIQUE column.
            target_pk_rows = target_columns[rows[0]["table"]]
            target_pk_columns = [
                target_row["name"] for target_row in sorted(target_pk_rows, key=itemgetter("pk")) if target_row["pk"]
            ]
            if len(rows) > 1:
                ordered_rows = sorted(rows, key=itemgetter("seq"))
                # "to" is NULL for a bare "REFERENCES target" - that names the target's own PK.
                referenced_columns = (
                    [row["to"] for row in ordered_rows]
                    if all(row["to"] is not None for row in ordered_rows)
                    else target_pk_columns
                )
                match = (
                    SchemaIntrospector.match_composite_foreign_key_naming(
                        list(member_columns), referenced_columns, target_pk_columns
                    )
                    if len(referenced_columns) == len(member_columns)
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
                        deferral=deferrals[foreign_key_id],
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
                deferral=deferrals[foreign_key_id],
            )
        return foreign_keys, composite_foreign_keys, unparsed_foreign_keys

    def get_extension_indexes(
        self,
        table: str,
        column_names: list[str],
        virtual_tables: list[dict[str, Any]],
        spatial_columns: list[dict[str, Any]],
    ) -> tuple[list[IndexInfo], set[str]]:
        """Map extension indexes and the triggers belonging to them."""
        indexes: list[IndexInfo] = []
        # A FullTextIndex is an index of the table, its triggers part of it.
        full_text_indexes = [
            index
            for row in virtual_tables
            if (index := self.introspector.get_full_text_index_info(table, row["name"], row["sql"])) is not None
        ]
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
        spatial_indexes = [
            IndexInfo(columns=[column_name], is_unique=False, index_type=SPATIALITE_INDEX_TYPE)
            for row in spatial_columns
            for column_name in column_names
            if column_name.lower() == row["f_geometry_column"].lower()
        ]
        indexes.extend(spatial_indexes)
        index_trigger_names.update(
            f"{prefix}_{table}_{index.columns[0]}".lower()
            for index in spatial_indexes
            for prefix in SPATIALITE_TRIGGER_NAME_PREFIXES
        )
        return indexes, index_trigger_names

    def get_triggers(
        self, trigger_rows: list[dict[str, Any]], index_trigger_names: set[str]
    ) -> tuple[list[Trigger], list[tuple[str, str]]]:
        """Reconstruct supported triggers and retain every unsupported definition."""
        triggers: list[Trigger] = []
        unparsed_triggers: list[tuple[str, str]] = []
        for row in trigger_rows:
            if row["name"] in index_trigger_names or row["name"].lower() in index_trigger_names:
                continue
            trigger = self.introspector._parse_sqlite_trigger_def(row["name"], row["sql"])
            if trigger is not None:
                triggers.append(trigger)
            else:
                unparsed_triggers.append((row["name"], row["sql"]))
        return triggers, unparsed_triggers
