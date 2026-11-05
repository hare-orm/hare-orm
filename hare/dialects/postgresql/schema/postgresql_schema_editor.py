from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.postgresql.schema.columns.postgresql_column_backfill import PostgresqlColumnBackfill
from hare.dialects.postgresql.schema.columns.postgresql_column_narrowing_check import PostgresqlColumnNarrowingCheck
from hare.dialects.postgresql.schema.columns.postgresql_column_type_changes import PostgresqlColumnTypeChanges
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_COLUMN_COMMENT_TEMPLATE,
    POSTGRESQL_CONCURRENT_INDEX_CREATE_TEMPLATE,
    POSTGRESQL_CONCURRENT_INDEX_DROP_TEMPLATE,
    POSTGRESQL_CONCURRENT_UNIQUE_INDEX_CREATE_TEMPLATE,
    POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE,
    POSTGRESQL_FUNCTION_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_GENERATED_PK_TEMPLATE,
    POSTGRESQL_GENERATED_SEQUENCE_TYPE_TEMPLATE,
    POSTGRESQL_ROLE_KEYWORDS,
    POSTGRESQL_SAFE_ADD_CONSTRAINT_BODY,
    POSTGRESQL_SEQUENCE_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_TABLE_COMMENT_TEMPLATE,
)
from hare.dialects.postgresql.schema.constraints.postgresql_constraint_statements import PostgresqlConstraintStatements
from hare.dialects.postgresql.schema.indexes.postgresql_index_statements import PostgresqlIndexStatements
from hare.dialects.postgresql.schema.relations.postgresql_foreign_key_rebuild import PostgresqlForeignKeyRebuild
from hare.dialects.postgresql.schema.runtime_statements.postgresql_table_clearing import PostgresqlTableClearing
from hare.dialects.postgresql.schema.runtime_statements.postgresql_table_locks import PostgresqlTableLocks
from hare.dialects.postgresql.schema.runtime_statements.postgresql_tenant_conditions import PostgresqlTenantConditions
from hare.dialects.postgresql.schema.schema_objects.postgresql_database_functions import PostgresqlDatabaseFunctions
from hare.dialects.postgresql.schema.schema_objects.postgresql_enum_types import PostgresqlEnumTypes
from hare.dialects.postgresql.schema.schema_objects.postgresql_extensions import PostgresqlExtensions
from hare.dialects.postgresql.schema.schema_objects.postgresql_function_arguments import PostgresqlFunctionArguments
from hare.dialects.postgresql.schema.schema_objects.postgresql_grants import PostgresqlGrants
from hare.dialects.postgresql.schema.schema_objects.postgresql_materialized_views import PostgresqlMaterializedViews
from hare.dialects.postgresql.schema.schema_objects.postgresql_row_level_security_policies import (
    PostgresqlRowLevelSecurityPolicies,
)
from hare.dialects.postgresql.schema.schema_objects.postgresql_schemas import PostgresqlSchemas
from hare.dialects.postgresql.schema.schema_objects.postgresql_sequences import PostgresqlSequences
from hare.dialects.postgresql.schema.schema_objects.postgresql_views import PostgresqlViews
from hare.dialects.postgresql.schema.tables.postgresql_table_comments import PostgresqlTableComments
from hare.dialects.postgresql.schema.tables.postgresql_table_partitions import PostgresqlTablePartitions
from hare.dialects.postgresql.schema.tables.postgresql_table_rebuild import PostgresqlTableRebuild
from hare.dialects.postgresql.schema.triggers.postgresql_trigger_statements import PostgresqlTriggerStatements
from hare.exceptions import ConfigurationError
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.models import Model
from hare.sql.identifiers import Identifiers


class PostgresqlSchemaEditor(BaseSchemaEditor):
    """The schema editor of PostgreSQL - the DDL of its tables, columns, indexes, constraints, partitions and
    schema objects."""

    column_type_changes_class = PostgresqlColumnTypeChanges
    column_backfill_class = PostgresqlColumnBackfill
    column_narrowing_check_class = PostgresqlColumnNarrowingCheck
    table_comments_class = PostgresqlTableComments
    table_partitions_class = PostgresqlTablePartitions
    table_rebuild_class = PostgresqlTableRebuild
    foreign_key_rebuild_class = PostgresqlForeignKeyRebuild
    foreign_key_rebuild: PostgresqlForeignKeyRebuild
    index_statements_class = PostgresqlIndexStatements
    constraint_statements_class = PostgresqlConstraintStatements
    trigger_statements_class = PostgresqlTriggerStatements
    views: PostgresqlViews
    views_class = PostgresqlViews
    materialized_views: PostgresqlMaterializedViews
    materialized_views_class = PostgresqlMaterializedViews
    database_functions_class = PostgresqlDatabaseFunctions
    sequences_class = PostgresqlSequences
    row_level_security_policies_class = PostgresqlRowLevelSecurityPolicies
    grants_class = PostgresqlGrants
    enum_types_class = PostgresqlEnumTypes
    enum_types: PostgresqlEnumTypes
    extensions_class = PostgresqlExtensions
    schemas_class = PostgresqlSchemas
    schemas: PostgresqlSchemas
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
    DROPS_DEFAULT_BEFORE_TYPE_CHANGE = True
    RENAME_INDEX_TEMPLATE: str | None = "ALTER INDEX {old_name} RENAME TO {new_name}"
    RENAME_INDEX_IF_EXISTS_TEMPLATE: str | None = "ALTER INDEX IF EXISTS {old_name} RENAME TO {new_name}"
    TRIGGER_FUNCTION_CREATE_TEMPLATE = (
        "CREATE FUNCTION {function_name}() RETURNS TRIGGER AS {body} LANGUAGE {language};"
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

    def get_anonymous_block_sql(self, body: str) -> str:
        """Quote a generated block with the dialect literal renderer, without a colliding dollar delimiter."""
        return f"DO {self.client.dialect.literals.get_string_literal_sql(body)}"

    def get_safe_add_constraint_sql(self, statement: str) -> str:
        return self.get_anonymous_block_sql(POSTGRESQL_SAFE_ADD_CONSTRAINT_BODY.format(statement=statement))

    def get_partitioned_options(self, model: type[Model]) -> PostgresqlTableOptions | None:
        """The table options of a model whose table is partitioned.

        Args:
            model: The model.

        Returns:
            The options, None for a plain table.
        """
        options = cast("PostgresqlTableOptions | None", model._meta.get_table_options(self.client.dialect))
        return options if options is not None and options.partitioning is not None else None

    def get_table_rename_plan(self, model: type[Model], old_name: str, new_name: str) -> SchemaChangePlan:
        """Prepare the table and partition renames before issuing any statement."""
        plan = super().get_table_rename_plan(model, old_name, new_name)
        options = self.get_partitioned_options(model)
        if options is None or old_name == new_name:
            return plan
        steps = list(plan.steps)
        for partition_name in cast("Partitioning", options.partitioning).get_partition_names():
            old_partition = Partitioning.get_partition_table_name(old_name, partition_name)
            new_partition = Partitioning.get_partition_table_name(new_name, partition_name)
            steps.append(
                SchemaStatement(
                    self.RENAME_TABLE_TEMPLATE.format(
                        old_table=self.qualify_table_name(old_partition, model._meta.schema),
                        new_table=self.quote(new_partition),
                    )
                )
            )
        return SchemaChangePlan(tuple(steps))

    def get_column_type_change_plan(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any], new_sql_type: str
    ) -> SchemaChangePlan:
        """Keep a generated integer key's owning sequence within the new integer range."""
        plan = super().get_column_type_change_plan(model, old_field, new_field, new_sql_type)
        if not (old_field.generated and new_field.generated and isinstance(new_field, IntField)):
            return plan
        if new_sql_type not in {"SMALLINT", "INT", "INTEGER", "BIGINT"}:
            return plan
        literal = self.client.dialect.literals.get_string_literal_sql
        sql = POSTGRESQL_GENERATED_SEQUENCE_TYPE_TEMPLATE.format(
            table_literal=literal(self.get_model_table_sql(model)),
            column_literal=literal(new_field.source_field or new_field.model_field_name),
            sql_type=new_sql_type,
        )
        return SchemaChangePlan((*plan.steps, SchemaStatement(sql, recovery=self.get_schema_recovery(model))))

    def get_add_index_plan(self, model: type[Model], index: Index, concurrently: bool = False) -> SchemaChangePlan:
        """Compile the entire index operation before executing its first statement.

        Concurrent creation on a partitioned table builds an empty parent index,
        creates each partition index concurrently and attaches it. The sequence
        requires a nontransactional migration and can leave an invalid parent or
        completed child indexes on failure; it does not promise atomic creation.
        """
        if not concurrently:
            return super().get_add_index_plan(model, index)
        template = (
            POSTGRESQL_CONCURRENT_UNIQUE_INDEX_CREATE_TEMPLATE
            if index.unique
            else POSTGRESQL_CONCURRENT_INDEX_CREATE_TEMPLATE
        )
        options = self.get_partitioned_options(model)
        if options is None:
            return SchemaChangePlan(
                (SchemaStatement(self.index_statements.get_index_create_sql(model, index, template=template)),)
            )
        schema = model._meta.schema
        index_name = self.index_statements.index_name_for_model(model, index)
        qualified_table = self.qualify_table_name(model._meta.db_table, schema)
        statements = [
            self.index_statements.get_index_create_sql(model, index, indexed_table_sql=f"ONLY {qualified_table}")
        ]
        for partition_name in cast("Partitioning", options.partitioning).get_partition_names():
            partition_table = Partitioning.get_partition_table_name(model._meta.db_table, partition_name)
            partition_index_name = Identifiers.get_within_limit(f"{partition_table}_{index_name}")
            statements.append(
                self.index_statements.get_index_create_sql(
                    model,
                    index,
                    partition_index_name,
                    self.qualify_table_name(partition_table, schema),
                    template=template,
                )
            )
            statements.append(
                f"ALTER INDEX {self.qualify_table_name(index_name, schema)} "
                f"ATTACH PARTITION {self.qualify_table_name(partition_index_name, schema)}"
            )
        return SchemaChangePlan(tuple(SchemaStatement(sql) for sql in statements))

    def get_remove_index_plan(self, model: type[Model], index: Index, concurrently: bool = False) -> SchemaChangePlan:
        """Compile ordinary removal for a partitioned parent, concurrent removal otherwise."""
        if not concurrently or self.get_partitioned_options(model) is not None:
            return super().get_remove_index_plan(model, index)
        index.raise_if_not_droppable(self.client.features, self.client.dialect)
        index_name = self.index_statements.index_name_for_model(model, index)
        return SchemaChangePlan(
            (
                SchemaStatement(
                    POSTGRESQL_CONCURRENT_INDEX_DROP_TEMPLATE.format(
                        name=self.qualify_table_name(index_name, model._meta.schema)
                    )
                ),
            )
        )

    def get_delete_model_plan(self, model: type[Model]) -> SchemaChangePlan:
        """Prepare table removal and cleanup of functions and sequences surviving DROP TABLE."""
        plan = super().get_delete_model_plan(model)
        statements = [
            self.TRIGGER_FUNCTION_DROP_TEMPLATE.format(
                function_name=self.qualify_table_name(trigger.function_name, model._meta.schema)
            )
            for trigger in model._meta.triggers
        ]
        statements.extend(
            POSTGRESQL_FUNCTION_DROP_IF_EXISTS_TEMPLATE.format(
                function=self.qualify_object_name(model, function.name),
                arguments=PostgresqlFunctionArguments.get_identity_sql(function.arguments),
            )
            for function in reversed(model._meta.functions)
        )
        statements.extend(
            POSTGRESQL_SEQUENCE_DROP_IF_EXISTS_TEMPLATE.format(sequence=self.qualify_object_name(model, sequence.name))
            for sequence in reversed(model._meta.sequences)
        )
        return SchemaChangePlan((*plan.steps, *(SchemaStatement(sql) for sql in statements)), before=plan.before)

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
