from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.base.schema.columns.column_conversion_check import ColumnConversionCheck
from hare.dialects.base.schema.columns.column_definitions import ColumnDefinitions
from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.base.schema.columns.column_renames import ColumnRenames
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing
from hare.dialects.base.schema.schema_objects.dictionaries import Dictionaries
from hare.dialects.base.schema.schema_objects.materialized_views import MaterializedViews
from hare.dialects.base.schema.schema_objects.schemas import Schemas
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.base.schema.tables.table_creation import TableCreation
from hare.dialects.base.schema.tables.table_partitions import TablePartitions
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.client.clickhouse_mutation_recovery import ClickhouseMutationRecovery
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_DEFAULT_DATABASE
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan
from hare.dialects.clickhouse.cluster.constants import CLICKHOUSE_SCHEMA_REPLICATING_DATABASE_ENGINES
from hare.dialects.clickhouse.cluster.messages import SCHEMA_DATABASE_ENGINE_REQUIRED
from hare.dialects.clickhouse.fields.fixed_string_field import FixedStringField
from hare.dialects.clickhouse.schema.columns.clickhouse_column_backfill import ClickhouseColumnBackfill
from hare.dialects.clickhouse.schema.columns.clickhouse_column_conversion_check import (
    ClickhouseColumnConversionCheck,
)
from hare.dialects.clickhouse.schema.columns.clickhouse_column_definitions import ClickhouseColumnDefinitions
from hare.dialects.clickhouse.schema.columns.clickhouse_column_narrowing_check import ClickhouseColumnNarrowingCheck
from hare.dialects.clickhouse.schema.columns.clickhouse_column_renames import ClickhouseColumnRenames
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_FIXED_STRING_TEXT_TEMPLATE,
    CLICKHOUSE_FIXED_STRING_TO_TEXT_TEMPLATE,
    CLICKHOUSE_TEXT_TYPE_NAME,
)
from hare.dialects.clickhouse.schema.indexes.clickhouse_index_statements import ClickhouseIndexStatements
from hare.dialects.clickhouse.schema.runtime_statements.clickhouse_table_clearing import ClickhouseTableClearing
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_dictionaries import ClickhouseDictionaries
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_materialized_views import ClickhouseMaterializedViews
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_schemas import ClickhouseSchemas
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_views import ClickhouseViews
from hare.dialects.clickhouse.schema.tables.clickhouse_table_comments import ClickhouseTableComments
from hare.dialects.clickhouse.schema.tables.clickhouse_table_creation import ClickhouseTableCreation
from hare.dialects.clickhouse.schema.tables.clickhouse_table_partitions import ClickhouseTablePartitions
from hare.dialects.clickhouse.schema.tables.clickhouse_table_rebuild import ClickhouseTableRebuild
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseSchemaEditor(BaseSchemaEditor):
    """ClickHouse's DDL - a table needs an engine, a column changes with ``MODIFY COLUMN``, and an
    index is a data skipping index of its table."""

    column_backfill_class: ClassVar[type[ColumnBackfill]] = ClickhouseColumnBackfill
    column_conversion_check_class: ClassVar[type[ColumnConversionCheck]] = ClickhouseColumnConversionCheck
    column_definitions_class: ClassVar[type[ColumnDefinitions]] = ClickhouseColumnDefinitions
    column_narrowing_check_class: ClassVar[type[ColumnNarrowingCheck]] = ClickhouseColumnNarrowingCheck
    column_renames_class: ClassVar[type[ColumnRenames]] = ClickhouseColumnRenames
    index_statements_class: ClassVar[type[IndexStatements]] = ClickhouseIndexStatements
    table_comments_class: ClassVar[type[TableComments]] = ClickhouseTableComments
    table_creation_class: ClassVar[type[TableCreation]] = ClickhouseTableCreation
    table_clearing_class: ClassVar[type[TableClearing]] = ClickhouseTableClearing
    table_partitions_class: ClassVar[type[TablePartitions]] = ClickhouseTablePartitions
    table_rebuild_class: ClassVar[type[TableRebuild]] = ClickhouseTableRebuild
    views_class: ClassVar[type[Views]] = ClickhouseViews
    materialized_views_class: ClassVar[type[MaterializedViews]] = ClickhouseMaterializedViews
    dictionaries_class: ClassVar[type[Dictionaries]] = ClickhouseDictionaries
    schemas_class: ClassVar[type[Schemas]] = ClickhouseSchemas

    client: ClickhouseClient
    column_definitions: ClickhouseColumnDefinitions
    dictionaries: ClickhouseDictionaries
    materialized_views: ClickhouseMaterializedViews
    schemas: ClickhouseSchemas
    table_creation: ClickhouseTableCreation
    table_partitions: ClickhouseTablePartitions
    table_rebuild: ClickhouseTableRebuild
    views: ClickhouseViews

    def __init__(
        self,
        connection: ClickhouseClient,
        atomic: bool = True,
        collect_sql: bool = False,
        *,
        database_engines: Mapping[str, str] | None = None,
    ) -> None:
        """Use live connection facts, or an explicit engine snapshot for offline compilation.

        The supplied snapshot describes database engines, not table engines. A
        connected editor reads the client's current snapshot so a schema refresh
        is visible to subsequent plans. No catalog access occurs during rendering.
        """
        super().__init__(connection, atomic, collect_sql)
        self.database_engines = MappingProxyType(dict(database_engines)) if database_engines is not None else None

    def get_schema_cluster(self, schema: str | None = None) -> str | None:
        """Choose schema placement from the target database before formatting SQL."""
        cluster = self.client.cluster
        if cluster is None:
            return None
        database = schema or cluster.database
        engine = self.get_database_engine(database)
        if engine is None:
            raise ConfigurationError(SCHEMA_DATABASE_ENGINE_REQUIRED.format(database=database))
        return None if engine in CLICKHOUSE_SCHEMA_REPLICATING_DATABASE_ENGINES else cluster.name

    def get_database_engine(self, schema: str | None = None) -> str | None:
        """Read a known database engine without performing catalog I/O during compilation."""
        engines = self.database_engines
        if engines is None and self.client.cluster is not None:
            engines = self.client.cluster.database_engines
        database = schema or self.client.database or CLICKHOUSE_DEFAULT_DATABASE
        return engines.get(database) if engines is not None else None

    def get_schema_cluster_sql(self, schema: str | None = None) -> str:
        return ClickhousePlacementPlan.get_cluster_sql(self.get_schema_cluster(schema), self.client.sql_context)

    def get_schema_recovery(self, model: type[Model]) -> ClickhouseMutationRecovery | None:
        """Recover local ALTER mutations; distributed DDL has no globally unique mutation IDs."""
        placement = self.get_schema_placement(model)
        if placement.cluster is not None:
            return None
        return ClickhouseMutationRecovery(
            placement.database or self.client.database or CLICKHOUSE_DEFAULT_DATABASE,
            placement.storage_table,
        )

    def get_schema_placement(self, model: type[Model]) -> ClickhousePlacementPlan:
        """Combine declared table identities with the target database's replication mode."""
        placement = self.get_row_placement(model)
        return replace(
            placement,
            replicates_schema=placement.cluster is not None and self.get_schema_cluster(placement.database) is None,
        )

    def get_schema_table_sql(self, table_name: str, schema: str | None = None) -> str:
        return self.qualify_table_name(table_name, schema) + self.get_schema_cluster_sql(schema)

    def get_table_rename_sql(self, old_name: str, new_name: str, schema: str | None = None) -> str:
        return self.RENAME_TABLE_TEMPLATE.format(
            old_table=self.qualify_table_name(old_name, schema),
            new_table=self.qualify_table_name(new_name, schema),
        ) + self.get_schema_cluster_sql(schema)

    def get_row_change_table_sql(self, model: type[Model]) -> str:
        """Render declared storage; mutations reach shards even when the database replicates DDL."""
        return self.get_row_placement(model).get_mutation_target_sql(self.client.sql_context)

    def get_row_recovery(self, model: type[Model]) -> ClickhouseMutationRecovery | None:
        return ClickhouseMutationRecovery.for_mutation(
            self.get_row_placement(model), self.client.database or CLICKHOUSE_DEFAULT_DATABASE
        )

    def get_row_placement(self, model: type[Model]) -> ClickhousePlacementPlan:
        """Use the current schema declaration for both rendering and failure metadata."""
        options = ClickhouseTableOptions.get_for_model(model, self.client.dialect)
        table = options.get_storage_table_name(model._meta.db_table)
        return ClickhousePlacementPlan(
            model._meta.db_table, table, model._meta.schema, self.client.sql_context.cluster_name
        )

    def get_column_change_sql(
        self, model: type[Model], template: str, *, storage_changes: str | None = None, **values: str
    ) -> str:
        """Change storage first and the logical companion second, without inspecting SQL."""
        placement = self.get_schema_placement(model)
        storage = placement.storage_table
        tables = dict.fromkeys((storage, placement.logical_table))
        cluster_sql = ClickhousePlacementPlan.get_cluster_sql(placement.schema_cluster, self.client.sql_context)
        return ";\n".join(
            template.format(
                table=self.qualify_table_name(table, placement.database) + cluster_sql,
                **(
                    values | {"changes": storage_changes}
                    if table == storage and storage_changes is not None
                    else values
                ),
            )
            for table in tables
        )

    def get_alter_field_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_name: str,
        candidate_referencing_models: Sequence[type[Model]] = (),
    ) -> SchemaChangePlan:
        """Compile dependent projections and dictionaries around the shared field plan."""
        names = self.table_partitions.get_projection_names_reading_retyped_column(old_model, new_model, field_name)
        plan = super().get_alter_field_plan(old_model, new_model, field_name, candidate_referencing_models)
        return SchemaChangePlan(
            (
                *(SchemaStatement(sql) for sql in self.table_partitions.get_projection_drop_sqls(old_model, names)),
                *plan.steps,
                *(
                    SchemaStatement(sql, recovery=self.get_schema_recovery(new_model))
                    for sql in self.table_partitions.get_projection_add_sqls(new_model, names)
                ),
                *(
                    SchemaStatement(sql)
                    for sql in self.dictionaries.get_retyped_field_dictionary_sqls(old_model, new_model, field_name)
                ),
            ),
            before=plan.before,
            after=plan.after,
        )

    def get_column_type_change_plan(
        self,
        model: type[Model],
        old_field: Field[Any],
        new_field: Field[Any],
        new_sql_type: str,
    ) -> SchemaChangePlan:
        steps = []
        if (
            isinstance(old_field, FixedStringField)
            and isinstance(new_field, FixedStringField)
            and new_field.length < old_field.length
        ):
            # A FixedString converts its padding zero bytes too - one shorter than its width never fits a
            # narrower FixedString: the text without them is taken through a String first.
            column = self.quote(new_field.source_field or new_field.model_field_name)
            steps.append(
                SchemaStatement(
                    self.get_column_change_sql(
                        model,
                        self.ALTER_FIELD_TEMPLATE,
                        changes=self.ALTER_FIELD_TYPE_TEMPLATE.format(
                            column=column,
                            sql_type=self.column_definitions.get_altered_column_type(
                                CLICKHOUSE_TEXT_TYPE_NAME, old_field.null
                            ),
                        ),
                        storage_changes=CLICKHOUSE_FIXED_STRING_TO_TEXT_TEMPLATE.format(
                            column=column,
                            sql_type=self.column_definitions.get_altered_column_type(
                                CLICKHOUSE_TEXT_TYPE_NAME, old_field.null
                            ),
                            text=CLICKHOUSE_FIXED_STRING_TEXT_TEMPLATE.format(column=column),
                        ),
                    ),
                    frozenset({SchemaEffect.REWRITE_ROWS}),
                    recovery=self.get_schema_recovery(model),
                )
            )
        return SchemaChangePlan(
            (
                *steps,
                *super().get_column_type_change_plan(model, old_field, new_field, new_sql_type).steps,
            )
        )

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
