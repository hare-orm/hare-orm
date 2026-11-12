from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DEFAULT_INDEX_TYPE,
    CLICKHOUSE_EPHEMERAL_COLUMN_TODO,
    CLICKHOUSE_EPHEMERAL_COLUMN_TYPE,
    CLICKHOUSE_GENERATED_COLUMN_TYPES,
    CLICKHOUSE_VIRTUAL_COLUMN_TYPE,
)
from hare.dialects.clickhouse.introspection.clickhouse_check_constraints import ClickhouseCheckConstraints
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo

if TYPE_CHECKING:
    from hare.dialects.clickhouse.clickhouse_introspector import ClickhouseIntrospector


class ClickhouseDeclarationMapper:
    """Reconstruct declarations independently of system-table IO and current settings."""

    def __init__(self, introspector: type[ClickhouseIntrospector]) -> None:
        self.introspector = introspector

    def get_tables(self, snapshot: CatalogSnapshot) -> list[TableInfo]:
        """Map original catalog text without mutating the reusable snapshot."""
        if not snapshot.tables:
            return []
        records = snapshot.copy_records()
        by_table = {row["table_name"]: row for row in records["tables"]}
        database = str(records["database"][0]["name"])
        settings = {str(row["name"]): str(row["value"]) for row in records["settings"]}
        return [self.build_table(by_table[table], snapshot.schema, database, settings) for table in snapshot.tables]

    def build_table(
        self, raw: dict[str, Any], schema: str, default_database: str, settings: dict[str, str]
    ) -> TableInfo:
        """Infer one table's declarations using only captured catalog records."""
        table = raw["table_name"]
        table_rows = raw["table_rows"]
        column_rows = raw["columns"]
        is_in_default_schema = schema == default_database
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
        for row in raw["indexes"]:
            expression = ClickhouseSqlParts.get_unwrapped(str(row["expr"]))
            # An index over columns lists them, comma-separated - a name quoted when it isn't plain
            # ASCII; one with a key of another expression is rebuilt from its keys' SQL, each kept whole.
            parts = [part.strip() for part in ClickhouseSqlParts.split(expression)]
            part_identifiers = [ClickhouseSqlParts.get_identifier(part) for part in parts]
            reads_columns_only = all(identifier in column_names for identifier in part_identifiers)
            index_columns = part_identifiers if reads_columns_only else []
            # A minmax index of granularity 1 is the one hare writes for an index of no type. Over one
            # column it is a field flag, as a plain single-column index is on the other databases.
            index_type = str(row["type"])
            granularity = int(row["granularity"])
            is_default_type = index_type == CLICKHOUSE_DEFAULT_INDEX_TYPE and granularity == 1
            if is_default_type and len(index_columns) == 1:
                column_indexes.append(IndexInfo(columns=index_columns, is_unique=False, name=str(row["name"])))  # type: ignore[arg-type]
            else:
                indexes.append(
                    IndexInfo(
                        columns=index_columns,  # type: ignore[arg-type]
                        is_unique=False,
                        name=str(row["name"]),
                        expression_terms=None if reads_columns_only else parts,
                        index_type="" if is_default_type else index_type,
                        storage_parameters={}
                        if is_default_type
                        else self.introspector.get_index_storage_parameters(
                            index_type, str(row["type_full"]), granularity
                        ),
                    )
                )
        indexed_columns = {index.columns[0] for index in column_indexes}
        columns = []
        for row in column_rows:
            db_type, nullable = self.introspector.unwrap_type(str(row["type"]))
            precision, scale = self.introspector.get_decimal_precision_scale(db_type)
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
                    db_default=self.introspector.get_column_default(str(row["default_expression"]), db_type)
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
            table_options=ClickhouseObservedTableOptions.get(
                default_database, settings, table_rows[0], column_rows, primary_key_columns
            )
            if table_rows
            else None,
        )
