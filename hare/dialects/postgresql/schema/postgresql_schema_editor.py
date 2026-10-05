from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.postgresql.schema.columns.postgresql_column_backfill import PostgresqlColumnBackfill
from hare.dialects.postgresql.schema.columns.postgresql_column_narrowing_check import PostgresqlColumnNarrowingCheck
from hare.dialects.postgresql.schema.columns.postgresql_column_type_changes import PostgresqlColumnTypeChanges
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_COLUMN_COMMENT_TEMPLATE,
    POSTGRESQL_CONCURRENT_INDEX_CREATE_PATTERN,
    POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE,
    POSTGRESQL_FUNCTION_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_GENERATED_PK_TEMPLATE,
    POSTGRESQL_MATERIALIZED_VIEW_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_ROLE_KEYWORDS,
    POSTGRESQL_SEQUENCE_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_TABLE_COMMENT_TEMPLATE,
    POSTGRESQL_VIEW_DROP_IF_EXISTS_TEMPLATE,
)
from hare.dialects.postgresql.schema.constraints.postgresql_constraint_names import PostgresqlConstraintNames
from hare.dialects.postgresql.schema.constraints.postgresql_constraint_statements import PostgresqlConstraintStatements
from hare.dialects.postgresql.schema.indexes.postgresql_index_statements import PostgresqlIndexStatements
from hare.dialects.postgresql.schema.relations.postgresql_foreign_key_rebuild import PostgresqlForeignKeyRebuild
from hare.dialects.postgresql.schema.runtime_statements.postgresql_table_clearing import PostgresqlTableClearing
from hare.dialects.postgresql.schema.runtime_statements.postgresql_table_locks import PostgresqlTableLocks
from hare.dialects.postgresql.schema.runtime_statements.postgresql_tenant_conditions import PostgresqlTenantConditions
from hare.dialects.postgresql.schema.schema_objects.postgresql_database_functions import PostgresqlDatabaseFunctions
from hare.dialects.postgresql.schema.schema_objects.postgresql_enum_types import PostgresqlEnumTypes
from hare.dialects.postgresql.schema.schema_objects.postgresql_extensions import PostgresqlExtensions
from hare.dialects.postgresql.schema.schema_objects.postgresql_grants import PostgresqlGrants
from hare.dialects.postgresql.schema.schema_objects.postgresql_materialized_views import PostgresqlMaterializedViews
from hare.dialects.postgresql.schema.schema_objects.postgresql_row_level_security_policies import (
    PostgresqlRowLevelSecurityPolicies,
)
from hare.dialects.postgresql.schema.schema_objects.postgresql_schemas import PostgresqlSchemas
from hare.dialects.postgresql.schema.schema_objects.postgresql_sequences import PostgresqlSequences
from hare.dialects.postgresql.schema.schema_objects.postgresql_views import PostgresqlViews
from hare.dialects.postgresql.schema.tables.postgresql_table_comments import PostgresqlTableComments
from hare.dialects.postgresql.schema.tables.postgresql_table_creation import PostgresqlTableCreation
from hare.dialects.postgresql.schema.tables.postgresql_table_partitions import PostgresqlTablePartitions
from hare.dialects.postgresql.schema.tables.postgresql_table_rebuild import PostgresqlTableRebuild
from hare.dialects.postgresql.schema.triggers.postgresql_trigger_statements import PostgresqlTriggerStatements
from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlSchemaEditor(BaseSchemaEditor):
    column_type_changes_class = PostgresqlColumnTypeChanges
    column_backfill_class = PostgresqlColumnBackfill
    column_narrowing_check_class = PostgresqlColumnNarrowingCheck
    table_creation_class = PostgresqlTableCreation
    table_rebuild_class = PostgresqlTableRebuild
    table_comments_class = PostgresqlTableComments
    table_partitions_class = PostgresqlTablePartitions
    foreign_key_rebuild_class = PostgresqlForeignKeyRebuild
    index_statements_class = PostgresqlIndexStatements
    constraint_statements_class = PostgresqlConstraintStatements
    constraint_names_class = PostgresqlConstraintNames
    trigger_statements_class = PostgresqlTriggerStatements
    views_class = PostgresqlViews
    materialized_views_class = PostgresqlMaterializedViews
    database_functions_class = PostgresqlDatabaseFunctions
    sequences_class = PostgresqlSequences
    row_level_security_policies_class = PostgresqlRowLevelSecurityPolicies
    grants_class = PostgresqlGrants
    enum_types_class = PostgresqlEnumTypes
    extensions_class = PostgresqlExtensions
    schemas_class = PostgresqlSchemas
    table_locks_class = PostgresqlTableLocks
    table_clearing_class = PostgresqlTableClearing
    tenant_conditions_class = PostgresqlTenantConditions

    INDEX_CREATE_TEMPLATE = "CREATE INDEX {exists}{index_name} ON {table_name} {index_type}({fields}){extra};"
    UNIQUE_INDEX_CREATE_TEMPLATE = INDEX_CREATE_TEMPLATE.replace("INDEX", "UNIQUE INDEX")
    EXCLUSION_CONSTRAINT_CREATE_TEMPLATE = POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE
    TABLE_COMMENT_TEMPLATE = POSTGRESQL_TABLE_COMMENT_TEMPLATE
    COLUMN_COMMENT_TEMPLATE = POSTGRESQL_COLUMN_COMMENT_TEMPLATE
    GENERATED_PK_TEMPLATE = POSTGRESQL_GENERATED_PK_TEMPLATE
    # USING casts explicitly - without it only a type with an implicit cast is accepted.
    ALTER_FIELD_TYPE_TEMPLATE = "ALTER COLUMN {column} TYPE {sql_type} USING {column}::{sql_type}"
    RENAME_INDEX_TEMPLATE: str | None = "ALTER INDEX {old_name} RENAME TO {new_name}"
    RENAME_INDEX_IF_EXISTS_TEMPLATE: str | None = "ALTER INDEX IF EXISTS {old_name} RENAME TO {new_name}"
    TRIGGER_FUNCTION_CREATE_TEMPLATE = (
        "CREATE FUNCTION {function_name}() RETURNS TRIGGER AS $trigger$\nBEGIN\n{body}\nEND;\n"
        "$trigger$ LANGUAGE {language};"
    )
    TRIGGER_FUNCTION_CREATE_OR_REPLACE_TEMPLATE = TRIGGER_FUNCTION_CREATE_TEMPLATE.replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    )
    TRIGGER_FUNCTION_DROP_TEMPLATE = "DROP FUNCTION {function_name}()"
    # {trigger_name}/{old_name}/{new_name} are passed already quoted.
    TRIGGER_CREATE_TEMPLATE = (
        "CREATE TRIGGER {trigger_name} {timing} {on} ON {table}\n"
        "    FOR EACH {for_each}{when_clause}\n"
        "    EXECUTE FUNCTION {function_name}();"
    )
    #: For Trigger.deferrable=True - a constraint trigger, always AFTER and FOR EACH ROW.
    TRIGGER_CONSTRAINT_CREATE_TEMPLATE = (
        "CREATE CONSTRAINT TRIGGER {trigger_name} {timing} {on} ON {table}\n"
        "    DEFERRABLE INITIALLY {initially}\n"
        "    FOR EACH {for_each}{when_clause}\n"
        "    EXECUTE FUNCTION {function_name}();"
    )
    TRIGGER_DROP_TEMPLATE = "DROP TRIGGER {trigger_name} ON {table}"
    TRIGGER_DROP_IF_EXISTS_TEMPLATE = "DROP TRIGGER IF EXISTS {trigger_name} ON {table};"
    RENAME_TRIGGER_TEMPLATE: str | None = "ALTER TRIGGER {old_name} ON {table} RENAME TO {new_name}"
    RENAME_TRIGGER_FUNCTION_TEMPLATE: str | None = "ALTER FUNCTION {function_name}() RENAME TO {new_function_name}"

    def __init__(self, connection: DatabaseClient, atomic: bool = True, collect_sql: bool = False) -> None:
        super().__init__(connection, atomic, collect_sql=collect_sql)
        self.comments_array: list[str] = []

    def get_partitioned_options(self, model: type[Model]) -> PostgresqlTableOptions | None:
        """The table options of a model whose table is partitioned.

        Args:
            model: The model.

        Returns:
            The options, None for a plain table.
        """
        options = cast("PostgresqlTableOptions | None", model._meta.get_table_options(self.client.dialect))
        return options if options is not None and options.partitioning is not None else None

    async def rename_table(self, model: type[Model], old_name: str, new_name: str) -> None:
        """Renames a table and, for a partitioned one, its partitions - named after it.

        Args:
            model: The model.
            old_name: The table's name.
            new_name: Its new name.
        """
        await super().rename_table(model, old_name, new_name)
        options = self.get_partitioned_options(model)
        if options is None or old_name == new_name:
            return
        for partition_name in cast("Partitioning", options.partitioning).get_partition_names():
            old_partition = Partitioning.get_partition_table_name(old_name, partition_name)
            new_partition = Partitioning.get_partition_table_name(new_name, partition_name)
            await self.run_sql(
                self.RENAME_TABLE_TEMPLATE.format(
                    old_table=self.qualify_table_name(old_partition, model._meta.schema),
                    new_table=self.quote(new_partition),
                )
            )

    async def add_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Creates an index. PostgreSQL can't build one concurrently on a partitioned table as a
        whole: the index is created on the table alone, built concurrently on each partition and
        attached, which makes the table's index valid once every partition has its own.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Build it without blocking writes.
        """
        if not concurrently:
            await super().add_index(model, index)
            return
        options = self.get_partitioned_options(model)
        if options is None:
            await self.run_sql(
                self._get_concurrent_index_create_sql(self.index_statements.get_index_create_sql(model, index))
            )
            return
        schema = model._meta.schema
        index_name = self.index_statements.index_name_for_model(model, index)
        qualified_table = self.qualify_table_name(model._meta.db_table, schema)
        await self.run_sql(
            self.index_statements.get_index_create_sql(model, index, indexed_table_sql=f"ONLY {qualified_table}")
        )
        for partition_name in cast("Partitioning", options.partitioning).get_partition_names():
            partition_table = Partitioning.get_partition_table_name(model._meta.db_table, partition_name)
            partition_index_name = Identifiers.get_within_limit(f"{partition_table}_{index_name}")
            partition_index_sql = self.index_statements.get_index_create_sql(
                model, index, partition_index_name, self.qualify_table_name(partition_table, schema)
            )
            await self.run_sql(self._get_concurrent_index_create_sql(partition_index_sql))
            await self.run_sql(
                f"ALTER INDEX {self.qualify_table_name(index_name, schema)} "
                f"ATTACH PARTITION {self.qualify_table_name(partition_index_name, schema)}"
            )

    async def remove_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Drops an index - the plain way on a partitioned table, whose index PostgreSQL can't
        drop concurrently.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Drop it without blocking reads and writes.
        """
        drop_sql = self.index_statements.get_index_drop_sql(model, index)
        if concurrently and self.get_partitioned_options(model) is None:
            drop_sql = drop_sql.replace("DROP INDEX ", "DROP INDEX CONCURRENTLY ", 1)
        await self.run_sql(drop_sql)

    @staticmethod
    def _get_concurrent_index_create_sql(index_sql: str) -> str:
        """Returns a ``CREATE INDEX`` statement building the index without blocking writes.

        Args:
            index_sql: The plain statement.

        Returns:
            The statement.
        """
        return POSTGRESQL_CONCURRENT_INDEX_CREATE_PATTERN.sub(r"\1CONCURRENTLY ", index_sql, count=1)

    async def delete_model(self, model: type[Model]) -> None:
        """Drops the model's views first - they read the table - then the table, then its triggers'
        functions, its own functions and its sequences: neither is owned by the table, so each
        outlives DROP TABLE unless dropped too (a sequence owned by a column already went with it)."""
        await self.drop_views_of_table(model)
        await super().delete_model(model)
        for trigger in model._meta.triggers:
            qualified_function = self.qualify_table_name(trigger.function_name, model._meta.schema)
            await self.run_sql(self.TRIGGER_FUNCTION_DROP_TEMPLATE.format(function_name=qualified_function))
        for function in reversed(model._meta.functions):
            await self.run_sql(
                POSTGRESQL_FUNCTION_DROP_IF_EXISTS_TEMPLATE.format(
                    function=self.qualify_object_name(model, function.name), arguments=", ".join(function.arguments)
                )
            )
        for sequence in reversed(model._meta.sequences):
            await self.run_sql(
                POSTGRESQL_SEQUENCE_DROP_IF_EXISTS_TEMPLATE.format(
                    sequence=self.qualify_object_name(model, sequence.name)
                )
            )

    async def drop_views_of_table(self, model: type[Model]) -> None:
        """Drops the views and materialized views a model declares, if they exist - before its
        table is dropped."""
        for materialized_view in reversed(model._meta.materialized_views):
            await self.run_sql(
                POSTGRESQL_MATERIALIZED_VIEW_DROP_IF_EXISTS_TEMPLATE.format(
                    view=self.qualify_object_name(model, materialized_view.name)
                )
            )
        for view in reversed(model._meta.views):
            await self.run_sql(
                POSTGRESQL_VIEW_DROP_IF_EXISTS_TEMPLATE.format(view=self.qualify_object_name(model, view.name))
            )

    def get_model_table_sql(self, model: type[Model]) -> str:
        """The model's quoted table name, in its schema."""
        return self.qualify_table_name(model._meta.db_table, model._meta.schema)

    @staticmethod
    def get_column_name(model: type[Model], field_name: str, owner: str) -> str:
        """The column of a field a declared object names.

        Args:
            model: The model.
            field_name: The field's name.
            owner: What names it - for the message.

        Raises:
            ConfigurationError: The model has no field of a column of that name.
        """
        column = model._meta.fields_db_projection.get(field_name)
        if column is None:
            raise ConfigurationError(f"{owner} names {field_name!r}, which is no column field of {model.__name__}")
        return column

    def get_roles_sql(self, roles: Sequence[str]) -> str:
        """The roles of a policy or grant - a special role as its keyword, any other quoted; every
        role (``PUBLIC``) for none."""
        if not roles:
            return "PUBLIC"
        return ", ".join(
            role.upper() if role.upper() in POSTGRESQL_ROLE_KEYWORDS else self.quote(role) for role in roles
        )
