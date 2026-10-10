from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.columns.column_definitions import ColumnDefinitions
from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing
from hare.dialects.base.schema.schema_objects.dictionaries import Dictionaries
from hare.dialects.base.schema.schema_objects.materialized_views import MaterializedViews
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.base.schema.tables.table_creation import TableCreation
from hare.dialects.base.schema.tables.table_partitions import TablePartitions
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.clickhouse.schema.columns.clickhouse_column_definitions import ClickhouseColumnDefinitions
from hare.dialects.clickhouse.schema.columns.clickhouse_column_narrowing_check import ClickhouseColumnNarrowingCheck
from hare.dialects.clickhouse.schema.indexes.clickhouse_index_statements import ClickhouseIndexStatements
from hare.dialects.clickhouse.schema.runtime_statements.clickhouse_table_clearing import ClickhouseTableClearing
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_dictionaries import ClickhouseDictionaries
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_materialized_views import ClickhouseMaterializedViews
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_views import ClickhouseViews
from hare.dialects.clickhouse.schema.tables.clickhouse_table_comments import ClickhouseTableComments
from hare.dialects.clickhouse.schema.tables.clickhouse_table_creation import ClickhouseTableCreation
from hare.dialects.clickhouse.schema.tables.clickhouse_table_partitions import ClickhouseTablePartitions
from hare.dialects.clickhouse.schema.tables.clickhouse_table_rebuild import ClickhouseTableRebuild

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseSchemaEditor(BaseSchemaEditor):
    """ClickHouse's DDL - a table needs an engine, a column changes with ``MODIFY COLUMN``, and an
    index is a data skipping index of its table."""

    column_definitions_class: ClassVar[type[ColumnDefinitions]] = ClickhouseColumnDefinitions
    column_narrowing_check_class: ClassVar[type[ColumnNarrowingCheck]] = ClickhouseColumnNarrowingCheck
    index_statements_class: ClassVar[type[IndexStatements]] = ClickhouseIndexStatements
    table_comments_class: ClassVar[type[TableComments]] = ClickhouseTableComments
    table_creation_class: ClassVar[type[TableCreation]] = ClickhouseTableCreation
    table_clearing_class: ClassVar[type[TableClearing]] = ClickhouseTableClearing
    table_partitions_class: ClassVar[type[TablePartitions]] = ClickhouseTablePartitions
    table_rebuild_class: ClassVar[type[TableRebuild]] = ClickhouseTableRebuild
    views_class: ClassVar[type[Views]] = ClickhouseViews
    materialized_views_class: ClassVar[type[MaterializedViews]] = ClickhouseMaterializedViews
    dictionaries_class: ClassVar[type[Dictionaries]] = ClickhouseDictionaries

    client: ClickhouseClient
    materialized_views: ClickhouseMaterializedViews

    # An automatic through table has no primary key: its rows are kept unsorted.
    MANY_TO_MANY_TABLE_TEMPLATE = (
        "CREATE TABLE {exists}{table_name} ({fields}) ENGINE = MergeTree ORDER BY tuple(){extra}{comment};"
    )
    RENAME_TABLE_TEMPLATE = "RENAME TABLE {old_table} TO {new_table}"
    DELETE_TABLE_TEMPLATE = "DROP TABLE {table}"
    DELETE_FIELD_TEMPLATE = "ALTER TABLE {table} DROP COLUMN {column}"
    ALTER_FIELD_TYPE_TEMPLATE = "MODIFY COLUMN {column} {sql_type}"
    # A column holds NULL by its type - its nullability changes with its type declared again.
    ALTER_FIELD_NULL_TEMPLATE = "MODIFY COLUMN {column} {sql_type}"
    ALTER_FIELD_NOT_NULL_TEMPLATE = "MODIFY COLUMN {column} {sql_type}"
    UPDATE_ROWS_TEMPLATE = "ALTER TABLE {table} UPDATE {assignments} WHERE {condition}"
    ALTER_FIELD_SET_DEFAULT_TEMPLATE = "MODIFY COLUMN {column} DEFAULT {default}"
    ALTER_FIELD_DROP_DEFAULT_TEMPLATE = "MODIFY COLUMN {column} REMOVE DEFAULT"
    # A column computed on write is stored with the row, one computed on read is an alias of its
    # expression - neither comes with SELECT *.
    STORED_GENERATED_COLUMN_TEMPLATE = "MATERIALIZED {expression}"
    VIRTUAL_GENERATED_COLUMN_TEMPLATE = "ALIAS {expression}"
    # An index is added as a change of its table: the server runs no CREATE INDEX over a cluster.
    INDEX_CREATE_TEMPLATE = "ALTER TABLE {table_name} ADD INDEX {exists}{index_name} ({fields}){index_type}{extra};"
    RENAME_CONSTRAINT_TEMPLATE = None
    DROP_INDEX_TEMPLATE = "ALTER TABLE {table} DROP INDEX {name}"
