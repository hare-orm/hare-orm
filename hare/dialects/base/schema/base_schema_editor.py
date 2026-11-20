from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import Any, ClassVar

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.ddl.schema_objects.view_creation_order import ViewCreationOrder
from hare.ddl.table_options import TableOptions
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.schema_cache_invalidation import SchemaCacheInvalidation
from hare.dialects.base.client.statement_recovery import StatementRecovery
from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.base.schema.columns.column_conversion_check import ColumnConversionCheck
from hare.dialects.base.schema.columns.column_definitions import ColumnDefinitions
from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.base.schema.columns.column_renames import ColumnRenames
from hare.dialects.base.schema.columns.column_type_changes import ColumnTypeChanges
from hare.dialects.base.schema.constants import (
    CHECK_CONSTRAINT_CREATE_TEMPLATE as SHARED_CHECK_CONSTRAINT_CREATE_TEMPLATE,
    FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE as SHARED_FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE,
    FOREIGN_KEY_TEMPLATE as SHARED_FOREIGN_KEY_TEMPLATE,
    GENERATED_PK_TEMPLATE as SHARED_GENERATED_PK_TEMPLATE,
    PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE as SHARED_PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE,
    UNIQUE_CONSTRAINT_CREATE_TEMPLATE as SHARED_UNIQUE_CONSTRAINT_CREATE_TEMPLATE,
)
from hare.dialects.base.schema.constraints.constraint_names import ConstraintNames
from hare.dialects.base.schema.constraints.constraint_statements import ConstraintStatements
from hare.dialects.base.schema.indexes.generated_index_names import GeneratedIndexNames
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.base.schema.plans.column_conversion_precondition import ColumnConversionPrecondition
from hare.dialects.base.schema.plans.column_default_backfill import ColumnDefaultBackfill
from hare.dialects.base.schema.plans.column_null_precondition import ColumnNullPrecondition
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.relations.foreign_key_rebuild import ForeignKeyRebuild
from hare.dialects.base.schema.relations.many_to_many_through_tables import ManyToManyThroughTables
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing
from hare.dialects.base.schema.runtime_statements.table_locks import TableLocks
from hare.dialects.base.schema.runtime_statements.tenant_conditions import TenantConditions
from hare.dialects.base.schema.schema_objects.database_functions import DatabaseFunctions
from hare.dialects.base.schema.schema_objects.dictionaries import Dictionaries
from hare.dialects.base.schema.schema_objects.enum_types import EnumTypes
from hare.dialects.base.schema.schema_objects.extensions import Extensions
from hare.dialects.base.schema.schema_objects.grants import Grants
from hare.dialects.base.schema.schema_objects.materialized_views import MaterializedViews
from hare.dialects.base.schema.schema_objects.row_level_security_policies import RowLevelSecurityPolicies
from hare.dialects.base.schema.schema_objects.schemas import Schemas
from hare.dialects.base.schema.schema_objects.sequences import Sequences
from hare.dialects.base.schema.schema_objects.views import Views
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.base.schema.tables.table_creation import TableCreation
from hare.dialects.base.schema.tables.table_partitions import TablePartitions
from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.base.schema.triggers.trigger_statements import TriggerStatements
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models import Model
from hare.models.model_binding import ModelBinding


class BaseSchemaEditor:
    """Writes the DDL of the models and of each migration operation: tables, fields, indexes,
    constraints, triggers, schemas, extensions and table rebuilds. A dialect subclasses it."""

    column_definitions_class: ClassVar[type[ColumnDefinitions]] = ColumnDefinitions
    column_type_changes_class: ClassVar[type[ColumnTypeChanges]] = ColumnTypeChanges
    column_backfill_class: ClassVar[type[ColumnBackfill]] = ColumnBackfill
    column_renames_class: ClassVar[type[ColumnRenames]] = ColumnRenames
    column_conversion_check_class: ClassVar[type[ColumnConversionCheck]] = ColumnConversionCheck
    column_narrowing_check_class: ClassVar[type[ColumnNarrowingCheck]] = ColumnNarrowingCheck
    table_creation_class: ClassVar[type[TableCreation]] = TableCreation
    table_rebuild_class: ClassVar[type[TableRebuild]] = TableRebuild
    table_comments_class: ClassVar[type[TableComments]] = TableComments
    table_partitions_class: ClassVar[type[TablePartitions]] = TablePartitions
    foreign_key_rebuild_class: ClassVar[type[ForeignKeyRebuild]] = ForeignKeyRebuild
    many_to_many_through_tables_class: ClassVar[type[ManyToManyThroughTables]] = ManyToManyThroughTables
    index_statements_class: ClassVar[type[IndexStatements]] = IndexStatements
    generated_index_names_class: ClassVar[type[GeneratedIndexNames]] = GeneratedIndexNames
    constraint_statements_class: ClassVar[type[ConstraintStatements]] = ConstraintStatements
    constraint_names_class: ClassVar[type[ConstraintNames]] = ConstraintNames
    trigger_statements_class: ClassVar[type[TriggerStatements]] = TriggerStatements
    views_class: ClassVar[type[Views]] = Views
    materialized_views_class: ClassVar[type[MaterializedViews]] = MaterializedViews
    dictionaries_class: ClassVar[type[Dictionaries]] = Dictionaries
    database_functions_class: ClassVar[type[DatabaseFunctions]] = DatabaseFunctions
    sequences_class: ClassVar[type[Sequences]] = Sequences
    row_level_security_policies_class: ClassVar[type[RowLevelSecurityPolicies]] = RowLevelSecurityPolicies
    grants_class: ClassVar[type[Grants]] = Grants
    enum_types_class: ClassVar[type[EnumTypes]] = EnumTypes
    extensions_class: ClassVar[type[Extensions]] = Extensions
    schemas_class: ClassVar[type[Schemas]] = Schemas
    table_locks_class: ClassVar[type[TableLocks]] = TableLocks
    table_clearing_class: ClassVar[type[TableClearing]] = TableClearing
    tenant_conditions_class: ClassVar[type[TenantConditions]] = TenantConditions

    TABLE_CREATE_TEMPLATE = "CREATE {prefix}TABLE {exists}{table_name} ({fields}){extra}{comment};"
    FIELD_TEMPLATE = "{name} {type}{nullable}{primary}{default}{comment}"
    #: The clause making a column one the database computes from an expression - on write, and on
    #: read.
    STORED_GENERATED_COLUMN_TEMPLATE = "GENERATED ALWAYS AS ({expression}) STORED"
    VIRTUAL_GENERATED_COLUMN_TEMPLATE = "GENERATED ALWAYS AS ({expression}) VIRTUAL"
    INDEX_CREATE_TEMPLATE = "CREATE {index_type}INDEX {exists}{index_name} ON {table_name} ({fields}){extra};"
    UNIQUE_INDEX_CREATE_TEMPLATE = INDEX_CREATE_TEMPLATE.replace("INDEX", "UNIQUE INDEX")
    UNIQUE_CONSTRAINT_CREATE_TEMPLATE = SHARED_UNIQUE_CONSTRAINT_CREATE_TEMPLATE
    PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE = SHARED_PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE
    CHECK_CONSTRAINT_CREATE_TEMPLATE = SHARED_CHECK_CONSTRAINT_CREATE_TEMPLATE
    GENERATED_PK_TEMPLATE = SHARED_GENERATED_PK_TEMPLATE
    FOREIGN_KEY_TEMPLATE = SHARED_FOREIGN_KEY_TEMPLATE
    FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE = SHARED_FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE
    MANY_TO_MANY_TABLE_TEMPLATE = "CREATE TABLE {exists}{table_name} ({fields}){extra}{comment};"
    RENAME_TABLE_TEMPLATE = "ALTER TABLE {old_table} RENAME TO {new_table}"
    DELETE_TABLE_TEMPLATE = "DROP TABLE {table} CASCADE"
    ADD_FIELD_TEMPLATE = "ALTER TABLE {table} ADD COLUMN {definition}"

    ALTER_FIELD_TEMPLATE = "ALTER TABLE {table} {changes}"
    # The column and name placeholders have no quotes - the caller passes quoted values.
    RENAME_FIELD_TEMPLATE = "ALTER TABLE {table} RENAME COLUMN {old_column} TO {new_column}"
    # The NULL/NOT NULL and type templates are also given the column's full type ({sql_type},
    # ColumnDefinitions.get_altered_column_type()) - for a database that changes a column by
    # declaring it again.
    ALTER_FIELD_NULL_TEMPLATE = "ALTER COLUMN {column} DROP NOT NULL"
    ALTER_FIELD_NOT_NULL_TEMPLATE = "ALTER COLUMN {column} SET NOT NULL"
    ALTER_FIELD_TYPE_TEMPLATE = "ALTER COLUMN {column} SET DATA TYPE {sql_type}"
    ALTER_FIELD_SET_DEFAULT_TEMPLATE = "ALTER COLUMN {column} SET DEFAULT {default}"
    ALTER_FIELD_DROP_DEFAULT_TEMPLATE = "ALTER COLUMN {column} DROP DEFAULT"

    DELETE_FIELD_TEMPLATE = "ALTER TABLE {table} DROP COLUMN {column} CASCADE"
    # An UPDATE of a table's stored rows the DDL makes - a backfill, a rewrite of stored values.
    UPDATE_ROWS_TEMPLATE = "UPDATE {table} SET {assignments} WHERE {condition}"
    #: A private staging table keeps original identities while stored keys change.
    CREATE_STORED_SNAPSHOT_TEMPLATE = "CREATE TABLE {table} AS {select}"

    DELETE_CONSTRAINT_TEMPLATE = "ALTER TABLE {table} DROP CONSTRAINT {name}"
    ADD_CONSTRAINT_TEMPLATE = "ALTER TABLE {table} ADD {constraint}"
    # Dropping and renaming an index name no table: {name}/{old_name} are passed schema-qualified,
    # {new_name} as a plain quoted name. DROP INDEX is also given the index's {table} - for a
    # database whose index belongs to its table.
    DROP_INDEX_TEMPLATE = "DROP INDEX {name}"
    #: How an index is renamed in place; None drops it and creates it again under the new name.
    RENAME_INDEX_TEMPLATE: str | None = None
    #: The same, doing nothing when the index doesn't exist; None renames it with rename_index().
    RENAME_INDEX_IF_EXISTS_TEMPLATE: str | None = None
    RENAME_CONSTRAINT_TEMPLATE: str | None = "ALTER TABLE {table} RENAME CONSTRAINT {old_name} TO {new_name}"
    #: Whether a column's type change casts its DEFAULT too, and fails on one the new type takes no cast
    #: of - the default is dropped first, the new one set after the change.
    DROPS_DEFAULT_BEFORE_TYPE_CHANGE: ClassVar[bool] = False

    def __init__(self, connection: DatabaseClient, atomic: bool = True, collect_sql: bool = False) -> None:
        self.client = connection
        self.atomic = atomic
        self.atomic_migration = connection.features.can_rollback_ddl and atomic
        self.collect_sql = collect_sql
        self.collected_sql: list[str] = []
        #: The statements run so far - a migration failing before its first one changed nothing.
        self.run_statement_count = 0
        self.column_definitions = self.column_definitions_class(self)
        self.column_type_changes = self.column_type_changes_class(self)
        self.column_backfill = self.column_backfill_class(self)
        self.column_renames = self.column_renames_class(self)
        self.column_conversion_check = self.column_conversion_check_class(self)
        self.column_narrowing_check = self.column_narrowing_check_class(self)
        self.table_creation = self.table_creation_class(self)
        self.table_rebuild = self.table_rebuild_class(self)
        self.table_comments = self.table_comments_class(self)
        self.table_partitions = self.table_partitions_class(self)
        self.foreign_key_rebuild = self.foreign_key_rebuild_class(self)
        self.many_to_many_through_tables = self.many_to_many_through_tables_class(self)
        self.index_statements = self.index_statements_class(self)
        self.generated_index_names = self.generated_index_names_class(self)
        self.constraint_statements = self.constraint_statements_class(self)
        self.constraint_names = self.constraint_names_class(self)
        self.trigger_statements = self.trigger_statements_class(self)
        self.views = self.views_class(self)
        self.materialized_views = self.materialized_views_class(self)
        self.dictionaries = self.dictionaries_class(self)
        self.database_functions = self.database_functions_class(self)
        self.sequences = self.sequences_class(self)
        self.row_level_security_policies = self.row_level_security_policies_class(self)
        self.grants = self.grants_class(self)
        self.enum_types = self.enum_types_class(self)
        self.extensions = self.extensions_class(self)
        self.schemas = self.schemas_class(self)

    def get_schema_table_sql(self, table_name: str, schema: str | None = None) -> str:
        """Render a physical DDL target, including dialect-specific placement.

        Callers select the physical table before calling this hook. Reads and row
        copies use qualify_table_name instead: a DDL target may contain clauses
        that are not part of an SQL identifier.
        """
        return self.qualify_table_name(table_name, schema)

    def get_column_change_sql(self, binding: ModelBinding, template: str, **values: str) -> str:
        """Render column DDL against the tables that expose the model's columns.

        Dialects with separate logical and storage tables can emit both changes.
        The template and its values remain separate until targets are selected.
        """
        return template.format(table=self.get_schema_table_sql(binding.db_table, binding.schema), **values)

    def get_table_rename_sql(self, old_name: str, new_name: str, schema: str | None = None) -> str:
        """Render a rename of physical tables in one namespace before execution."""
        return self.RENAME_TABLE_TEMPLATE.format(
            old_table=self.qualify_table_name(old_name, schema), new_table=self.quote(new_name)
        )

    def get_row_change_table_sql(self, binding: ModelBinding) -> str:
        """Render the target of a migration's row mutation, separately from its read source."""
        return self.qualify_table_name(binding.db_table, binding.schema)

    def get_storage_table_name(self, binding: ModelBinding) -> str:
        """The physical DDL target before qualification."""
        return TableOptions.get_effective_storage_table_name(
            binding.db_table, binding.get_table_options(self.client.dialect)
        )

    def get_storage_table_sql(self, binding: ModelBinding) -> str:
        """Render the physical DDL target for indexes, constraints and storage properties."""
        return self.get_schema_table_sql(self.get_storage_table_name(binding), binding.schema)

    def get_schema_recovery(self, binding: ModelBinding) -> StatementRecovery | None:
        """Compile failure metadata for schema commands that may rewrite stored rows."""
        return None

    def get_row_recovery(self, binding: ModelBinding) -> StatementRecovery | None:
        """Compile the same mutation target used by a migration's row assignment."""
        return None

    async def prepare_schema(self) -> None:
        """Prepare dialect catalog facts before an asynchronous schema operation compiles SQL.

        The base editor needs no additional facts. Dialects load their connection-specific
        metadata here; synchronous plan builders and offline SQL collection perform no I/O.
        """

    @asynccontextmanager
    async def schema_plan_scope(self, plan: SchemaChangePlan) -> AsyncGenerator[None]:
        """Prepare and check runtime requirements before any step changes the database.

        Dialects inspect the complete plan's effects before yielding and complete any resources
        they acquired afterwards. The base scope has no requirements; previews perform no I/O.
        """
        yield

    async def run_sql(self, sql: str, *, recovery: StatementRecovery | None = None) -> None:
        """Runs a DDL statement - a dialect's editor runs it its own way where it needs to.

        If ``collect_sql`` is True, append the SQL to ``collected_sql`` instead
        of executing it.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        self.run_statement_count += 1
        async with SchemaCacheInvalidation.run(self.client):
            await self.client.execute_built_script(sql, recovery=recovery)

    async def run_raw_sql(self, sql: str) -> None:
        """Executes SQL a migration's author wrote (``RunSQL``) as it is written.

        If ``collect_sql`` is True, append the SQL to ``collected_sql`` instead of executing it.

        Args:
            sql: The SQL.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        self.run_statement_count += 1
        async with SchemaCacheInvalidation.run(self.client):
            await self.client.execute_script(sql)

    async def run_parameterized_sql(
        self,
        sql: str,
        parameters: Sequence[Any],
        *,
        schema_change: bool = False,
        recovery: StatementRecovery | None = None,
    ) -> int:
        """Runs a statement of a migration that changes rows, with its parameters - a backfill's batch, a
        ``RunSQL`` pair - counted as a statement run, as ``run_sql()`` counts its own.

        Args:
            sql: The statement.
            parameters: Its parameters.
            schema_change: Whether this opaque statement may change the schema. Backfill batches
                keep their prepared statements; parameterized RunSQL requests invalidation.
            recovery: The optional recovery policy for this statement.

        Returns:
            The number of rows it changed.
        """
        self.run_statement_count += 1
        async with SchemaCacheInvalidation.run(self.client, schema_change=schema_change):
            rowcount, _ = await self.client.execute(sql, list(parameters), recovery=recovery)
        return rowcount or 0

    @asynccontextmanager
    async def constraint_checking_disabled(self) -> AsyncGenerator[bool]:
        """No-op by default - only an editor rebuilding tables to change their columns needs this; one
        whose ALTER TABLE changes everything in place never drops and recreates a table.

        Yields:
            Whether this scope owes a compensating check before restoring enforcement.
        """
        yield False

    async def check_constraints(self) -> None:
        """Verifies constraints left unchecked by ``constraint_checking_disabled()``. No-op by
        default - only a backend that actually disables enforcement needs it."""
        return

    def quote(self, name: str) -> str:
        """Quotes a name for this connection's dialect."""
        return self.client.dialect.literals.quote_identifier(name)

    def qualify_table_name(self, table_name: str, schema: str | None = None) -> str:
        """Quotes a table name, with its schema where the dialect has schemas."""
        return self.client.dialect.literals.qualify_table_name(table_name, schema)

    def qualify_object_name(self, binding: ModelBinding, name: str) -> str:
        """The quoted name of an object a model declares, in the model's schema."""
        return self.qualify_table_name(name, binding.schema)

    def get_index_name_sql(self, index_name: str, schema: str | None) -> str:
        """The quoted name a new index or trigger of a table in ``schema`` is created under - bare: the
        database puts it in its table's schema.

        Args:
            index_name: The index's or trigger's name.
            schema: Its table's schema.

        Returns:
            The quoted name.
        """
        return self.quote(index_name)

    def get_indexed_table_sql(self, table_name: str, schema: str | None) -> str:
        """The quoted table a new index or trigger is created on.

        Args:
            table_name: The table.
            schema: Its schema.

        Returns:
            The quoted table, with its schema.
        """
        return self.qualify_table_name(table_name, schema)

    def get_referenced_table_sql(self, table_name: str, schema: str | None) -> str:
        """The quoted table a foreign key references.

        Args:
            table_name: The referenced table.
            schema: Its schema.

        Returns:
            The quoted table, with its schema.
        """
        return self.qualify_table_name(table_name, schema)

    def get_view_query_sql(self, view: View) -> str:
        """A view's query for this connection, without a closing semicolon."""
        return view.get_query_sql(self.client).strip().rstrip(";").rstrip()

    def get_safe_add_constraint_sql(self, statement: str) -> str:
        """Make constraint creation conditional where the dialect supports it."""
        return statement

    async def create_model(self, binding: ModelBinding) -> None:
        """Creates a model's table with its indexes, triggers and constraints, and its automatic
        through tables.

        Args:
            binding: The model.
        """
        await self.prepare_schema()
        await self.table_creation.get_create_plan(binding).execute(self)

    async def rename_table(self, binding: ModelBinding, old_name: str, new_name: str) -> None:
        """Renames a model's table in its schema.

        Args:
            binding: The model.
            old_name: The table's name.
            new_name: Its new name.
        """
        await self.prepare_schema()
        await self.get_table_rename_plan(binding, old_name, new_name).execute(self)

    def get_table_rename_plan(self, binding: ModelBinding, old_name: str, new_name: str) -> SchemaChangePlan:
        """Prepare the rename before executing it or any dependent-object changes."""
        if old_name == new_name:
            return SchemaChangePlan(())
        schema = binding.schema
        return SchemaChangePlan((SchemaStatement(self.get_table_rename_sql(old_name, new_name, schema)),))

    async def delete_model(self, binding: ModelBinding) -> None:
        """Drops a model's table, with the views, dictionaries and through tables over it, and the table its
        rows are stored in when it is another.

        Args:
            binding: The model.
        """
        await self.prepare_schema()
        await self.get_delete_model_plan(binding).execute(self)

    def get_model_view_drop_sqls(self, binding: ModelBinding, *, if_exists: bool = True) -> list[str]:
        """Drop dependent views before their sources, across ordinary and materialized views."""
        ordered = ViewCreationOrder.get_ordered(
            [*binding.views, *binding.materialized_views],
            lambda view: view,
            get_schema=lambda _view: binding.schema,
            client=self.client,
        )
        return [
            self.materialized_views.get_materialized_view_drop_sql(binding, view, if_exists=if_exists)
            if isinstance(view, MaterializedView)
            else self.views.get_view_drop_sql(binding, view, if_exists=if_exists)
            for view in reversed(ordered)
        ]

    def get_delete_model_plan(self, binding: ModelBinding) -> SchemaChangePlan:
        """Prepare dependent-object, through-table and physical-table removals together."""
        schema = binding.schema
        steps: list[SchemaStep] = [
            SchemaStatement(sql)
            for sql in (
                *self.get_model_view_drop_sqls(binding),
                *self.dictionaries.get_model_drop_sqls(binding),
            )
        ]
        steps.extend(self.many_to_many_through_tables.get_model_through_drop_plan(binding).steps)
        steps.append(
            SchemaStatement(
                self.DELETE_TABLE_TEMPLATE.format(table=self.get_schema_table_sql(binding.db_table, schema))
            )
        )
        table_options = binding.get_table_options(self.client.dialect)
        if table_options is not None:
            storage_table_name = table_options.get_storage_table_name(binding.db_table)
            if storage_table_name != binding.db_table:
                steps.append(
                    SchemaStatement(
                        self.DELETE_TABLE_TEMPLATE.format(table=self.get_schema_table_sql(storage_table_name, schema))
                    )
                )
        return SchemaChangePlan(tuple(steps), before=self.table_creation.get_definition(binding))

    def get_composite_field_addition_plan(
        self, binding: ModelBinding, field: ForeignKeyFieldInstance[Model], *, foreign_key_not_valid: bool = False
    ) -> SchemaChangePlan:
        """Prepare every key column, constraint and index of an added composite relation."""
        steps: list[SchemaStep] = [
            SchemaStatement(
                self.get_column_change_sql(
                    binding,
                    self.ADD_FIELD_TEMPLATE,
                    definition=self.column_definitions.get_column_definition(binding, name),
                )
            )
            for name in field.source_fields
        ]
        if self.foreign_key_rebuild.creates_foreign_key(field):
            constraint = self.table_creation.get_foreign_key_constraint(binding, field)
            constraint_plan = (
                self.foreign_key_rebuild.get_foreign_key_not_valid_plan(binding, constraint)
                if foreign_key_not_valid
                else self.get_add_constraint_plan(binding, constraint)
            )
            steps.extend(constraint_plan.steps)
        if isinstance(field, OneToOneFieldInstance):
            steps.extend(self.get_add_constraint_plan(binding, UniqueConstraint(fields=field.source_fields)).steps)
        steps.extend(self.get_field_indexes_plan(binding, field).steps)
        return SchemaChangePlan(tuple(steps))

    async def add_field(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        field_name: str,
        *,
        foreign_key_not_valid: bool = False,
    ) -> None:
        """Adds a field's column (or a many-to-many relation's through table) to an existing table, then its
        uniqueness and index.

        Args:
            old_binding: The binding of the model rendered from the state without the field - the table as it is.
            new_binding: The binding of the model rendered from the state with the field.
            field_name: The field's name.
            foreign_key_not_valid: For a relation with a database constraint: add the FOREIGN KEY
                without checking the existing rows (``get_foreign_key_not_valid_plan()``) -
                a later ``get_constraint_validation_plan()`` checks them.
        """
        await self.prepare_schema()
        await self.get_add_field_plan(
            old_binding, new_binding, field_name, foreign_key_not_valid=foreign_key_not_valid
        ).execute(self)

    def get_add_field_plan(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        field_name: str,
        *,
        foreign_key_not_valid: bool = False,
    ) -> SchemaChangePlan:
        """Prepare addition and its backfill before executing any schema command.

        A NOT NULL backfill may end in a complete table rebuild. Such a completion
        carries a target table definition and already creates the declared indexes;
        an in-place completion leaves those to the final field-index steps.
        """
        field = new_binding.fields_map[field_name]
        if isinstance(field, ManyToManyFieldInstance):
            sql = self.table_creation.get_many_to_many_table_definition(new_binding, field)
            return SchemaChangePlan((SchemaStatement(sql),) if sql else ())
        if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
            return self.get_composite_field_addition_plan(
                new_binding, field, foreign_key_not_valid=foreign_key_not_valid
            )
        needs_backfill = False
        unvalidated_foreign_key = None
        if isinstance(field, ForeignKeyFieldInstance):
            column_field_name = field.source_field or field_name
            with_reference = not (foreign_key_not_valid and self.foreign_key_rebuild.creates_foreign_key(field))
            definition = self.column_definitions.get_column_definition(
                new_binding, column_field_name, with_reference=with_reference
            )
            if not with_reference:
                unvalidated_foreign_key = self.table_creation.get_foreign_key_constraint(new_binding, field)
        else:
            column_field_name = field_name
            needs_backfill = self.column_backfill.needs_added_column_backfill(field)
            definition = self.column_definitions.get_column_definition(
                new_binding, field_name, nullable=field.null or needs_backfill
            )
        db_field = new_binding.fields_db_projection[column_field_name]
        steps: list[SchemaStep] = [
            SchemaStatement(self.get_column_change_sql(old_binding, self.ADD_FIELD_TEMPLATE, definition=definition))
        ]
        steps.extend(
            SchemaStatement(sql)
            for sql in self.table_comments.get_model_comment_statements(new_binding, (column_field_name,))
        )
        rebuilt = False
        if needs_backfill:
            steps.extend(self.column_backfill.get_added_column_backfill_plan(new_binding, field, db_field).steps)
            completion = self.column_backfill.get_added_column_not_null_plan(new_binding, field, db_field)
            steps.extend(completion.steps)
            rebuilt = completion.after is not None
        elif not isinstance(field, ForeignKeyFieldInstance):
            steps.extend(self.column_backfill.get_added_column_default_plan(new_binding, field, db_field).steps)
        if unvalidated_foreign_key is not None:
            steps.extend(
                self.foreign_key_rebuild.get_foreign_key_not_valid_plan(new_binding, unvalidated_foreign_key).steps
            )
        if not rebuilt:
            for constraint in self.table_creation.get_field_unique_constraints(new_binding):
                if constraint.fields == (db_field,):
                    steps.extend(self.get_add_constraint_plan(new_binding, constraint).steps)
            steps.extend(self.get_field_indexes_plan(new_binding, field).steps)
        return SchemaChangePlan(
            tuple(steps),
            before=self.table_creation.get_definition(old_binding),
            after=self.table_creation.get_definition(new_binding),
        )

    def get_field_indexes_plan(self, binding: ModelBinding, field: Field[Any]) -> SchemaChangePlan:
        """Compile the ordinary indexes implied by the field or its relation keys."""
        return SchemaChangePlan(
            tuple(
                step
                for index in self.index_statements.get_field_indexes(binding, field)
                for step in self.get_add_index_plan(binding, index).steps
            )
        )

    def raise_if_generated_field_changes(self, old_field: Field[Any], new_field: Field[Any]) -> None:
        if old_field.pk or new_field.pk:
            return

        old_generated_sql = old_field.get_generated_sql(self.client.dialect) if old_field.generated else None
        new_generated_sql = new_field.get_generated_sql(self.client.dialect) if new_field.generated else None
        # The generated SQL doesn't show output_field's type - the column type is compared too, so a
        # change of output_field alone is seen.
        old_sql_type = old_field.get_column_type(self.client.dialect) if old_field.generated else None
        new_sql_type = new_field.get_column_type(self.client.dialect) if new_field.generated else None
        if old_generated_sql == new_generated_sql and old_sql_type == new_sql_type:
            return
        if old_field.generated or new_field.generated:
            raise ConfigurationError(
                f"Modifying generated fields is not supported - the field {new_field} "
                "must be removed and re-added with the new definition."
            )
        return

    async def alter_column(self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any]) -> None:
        """Changes a field's column in place: its name, type, nullability, indexes, uniqueness,
        comment and default.

        Args:
            binding: The model, as it is after the change.
            old_field: The field before the change.
            new_field: The field after it.

        Raises:
            ConfigurationError: A generated field changes.
        """
        await self.prepare_schema()
        await self.get_alter_column_plan(binding, old_field, new_field).execute(self)

    def get_alter_column_plan(
        self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any]
    ) -> SchemaChangePlan:
        """Compile conversion checks, DDL, defaults and dependent indexes before execution."""
        self.raise_if_generated_field_changes(old_field, new_field)
        old_column = old_field.source_field or old_field.model_field_name
        new_column = new_field.source_field or new_field.model_field_name
        table = self.qualify_table_name(binding.db_table, binding.schema)
        old_type = old_field.get_column_type(self.client.dialect)
        new_type = new_field.get_column_type(self.client.dialect)
        steps: list[SchemaStep] = []
        if old_type != new_type or (
            self.client.features.truncates_values_on_type_change
            and self.column_narrowing_check.has_narrowing_limits(old_field, new_field)
        ):
            steps.extend(self.get_column_conversion_steps(binding, old_field, new_field, self.quote(old_column)))
        rebuild = self.table_rebuild.get_column_change_rebuild_plan(binding, old_field, new_field)
        if rebuild is not None:
            return SchemaChangePlan((*steps, *rebuild.steps), before=rebuild.before, after=rebuild.after)
        if old_column != new_column:
            steps.append(
                SchemaStatement(
                    self.get_column_change_sql(
                        binding,
                        self.RENAME_FIELD_TEMPLATE,
                        old_column=self.quote(old_column),
                        new_column=self.quote(new_column),
                    )
                )
            )
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        held_indexes = []
        if not self.client.features.alters_indexed_columns and (
            old_type != new_type or old_field.null != new_field.null
        ):
            held_indexes = self.index_statements.get_indexes_covering_field(
                binding, new_field, old_indexed, adds_field_index=new_indexed and not old_indexed
            )
            for index in held_indexes:
                old_index = self.index_statements.get_index_before_column_rename(
                    binding, index, old_column, new_column
                )
                steps.extend(self.get_remove_index_plan(binding, old_index).steps)
        steps.extend(
            self.get_column_index_removal_plan(binding, old_field, new_field, holds_indexes=bool(held_indexes)).steps
        )
        generated_fields = []
        if old_type != new_type:
            if self.DROPS_DEFAULT_BEFORE_TYPE_CHANGE and old_field.has_db_default():
                steps.append(
                    SchemaStatement(
                        self.get_column_change_sql(
                            binding,
                            self.ALTER_FIELD_TEMPLATE,
                            changes=self.ALTER_FIELD_DROP_DEFAULT_TEMPLATE.format(column=self.quote(new_column)),
                        )
                    )
                )
                old_field = copy(old_field)
                old_field.db_default = DB_DEFAULT_NOT_SET
            generated_fields = self.column_type_changes.get_generated_fields_depending_on_column(binding, new_column)
            for field in generated_fields:
                steps.extend(self.get_remove_field_plan(binding, binding, field).steps)
            steps.extend(self.get_column_type_change_plan(binding, old_field, new_field, new_type).steps)
        if old_field.null != new_field.null:
            steps.extend(self.get_nullability_change_plan(binding, table, old_field, new_field, new_type).steps)
        if old_field.description != new_field.description:
            steps.extend(
                SchemaStatement(sql) for sql in self.table_comments.get_column_comment_change_sqls(binding, new_field)
            )
        default_sql = self._get_default_change_sql(binding, old_field, new_field)
        if default_sql is not None:
            steps.append(SchemaStatement(default_sql))
        steps.extend(self.get_column_index_addition_plan(binding, old_field, new_field).steps)
        steps.extend(
            step
            for position, index in enumerate(held_indexes)
            if position or not old_indexed or new_indexed
            for step in self.get_add_index_plan(binding, index).steps
        )
        for field in generated_fields:
            steps.extend(self.get_add_field_plan(binding, binding, field.model_field_name).steps)
        return SchemaChangePlan(tuple(steps))

    def get_column_conversion_steps(
        self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any], column_sql: str
    ) -> tuple[SchemaStep, ...]:
        """Prepare a check of existing values before a type change."""
        return (
            ColumnConversionPrecondition(
                self.qualify_table_name(binding.db_table, binding.schema),
                column_sql,
                copy(old_field),
                copy(new_field),
                self.client.features.truncates_values_on_type_change,
            ),
        )

    def get_column_index_removal_plan(
        self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any], *, holds_indexes: bool
    ) -> SchemaChangePlan:
        """Drops the column's own index and its unique constraint the change takes away - and renames the
        ones it keeps after a renamed column, their generated names made from the column.

        Args:
            binding: The model, the column already renamed.
            old_field: The field before the change.
            new_field: The field after it.
            holds_indexes: Whether the column's indexes were dropped for the change - its own index
                then isn't there to drop or rename.
        """
        steps: list[SchemaStep] = []
        old_db_field = old_field.source_field or old_field.model_field_name
        new_db_field = new_field.source_field or new_field.model_field_name
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if old_indexed and not holds_indexes:
            field_index = Index(fields=(new_db_field,))
            old_field_index = self.index_statements.get_index_before_column_rename(
                binding, field_index, old_db_field, new_db_field
            )
            if not new_indexed:
                # A Meta.indexes entry over the column under the same name keeps the index.
                if not self.index_statements.is_declared_by_another_entry(
                    binding, field_index, model_declares_index=False
                ):
                    steps.extend(self.get_remove_index_plan(binding, old_field_index).steps)
            elif old_field_index is not field_index:
                steps.extend(self.get_rename_index_plan(binding, old_field_index, field_index).steps)
        if old_field.unique and not new_field.unique:
            steps.extend(self.get_remove_constraint_plan(binding, UniqueConstraint(fields=(old_db_field,))).steps)
        elif old_field.unique and not old_field.pk and not new_field.pk and old_db_field != new_db_field:
            # A primary key's uniqueness is the key itself - it has no unique constraint to rename.
            steps.extend(
                self.get_rename_constraint_plan(
                    binding, UniqueConstraint(fields=(old_db_field,)), UniqueConstraint(fields=(new_db_field,))
                ).steps
            )
        return SchemaChangePlan(tuple(steps))

    def get_column_index_addition_plan(
        self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any]
    ) -> SchemaChangePlan:
        """Adds the column's own index and its unique constraint the change brings.

        Args:
            binding: The model.
            old_field: The field before the change.
            new_field: The field after it.
        """
        steps: list[SchemaStep] = []
        new_db_field = new_field.source_field or new_field.model_field_name
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if new_indexed and not old_indexed:
            field_index = Index(fields=(new_db_field,))
            # A Meta.indexes entry over the column under the same name made the index already.
            if not self.index_statements.is_declared_by_another_entry(binding, field_index, model_declares_index=True):
                steps.extend(self.get_add_index_plan(binding, field_index).steps)
        if new_field.unique and not old_field.unique:
            steps.extend(self.get_add_constraint_plan(binding, UniqueConstraint(fields=(new_db_field,))).steps)
        return SchemaChangePlan(tuple(steps))

    def get_column_type_change_plan(
        self,
        binding: ModelBinding,
        old_field: Field[Any],
        new_field: Field[Any],
        new_sql_type: str,
    ) -> SchemaChangePlan:
        """Compile the type statement with its actual row-rewrite effect."""
        column = self.quote(new_field.source_field or new_field.model_field_name)
        changes = self.column_type_changes.get_type_change_sql(
            old_field,
            new_field,
            column,
            self.column_definitions.get_altered_column_type(new_sql_type, old_field.null),
        )
        effects = (
            frozenset({SchemaEffect.REWRITE_ROWS})
            if self.column_type_changes.column_type_change_rewrites_table(
                old_field.get_column_type(self.client.dialect), new_sql_type
            )
            else frozenset()
        )
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.get_column_change_sql(binding, self.ALTER_FIELD_TEMPLATE, changes=changes),
                    effects,
                    recovery=self.get_schema_recovery(binding),
                ),
            )
        )

    def get_nullability_change_plan(
        self,
        binding: ModelBinding,
        qualified_table: str,
        old_field: Field[Any],
        new_field: Field[Any],
        new_sql_type: str,
    ) -> SchemaChangePlan:
        """Prepare the value check or backfill and the final nullability statement.

        Rebuild defaults keep their established contract: explicit callables are
        refused during preparation, while clock defaults are sampled at execution.
        """
        column = self.quote(new_field.source_field or new_field.model_field_name)
        if new_field.null:
            return SchemaChangePlan(
                (
                    SchemaStatement(
                        self.get_column_change_sql(
                            binding,
                            self.ALTER_FIELD_TEMPLATE,
                            changes=self.ALTER_FIELD_NULL_TEMPLATE.format(
                                column=column,
                                sql_type=self.column_definitions.get_altered_column_type(new_sql_type, True),
                            ),
                        ),
                        recovery=self.get_schema_recovery(binding),
                    ),
                )
            )
        steps: list[SchemaStep] = []
        backfill = self.column_backfill
        if new_field.default is None and not new_field.has_db_default() and backfill.is_auto_now_field(new_field):
            steps.append(
                ColumnDefaultBackfill(
                    binding,
                    copy(new_field),
                    self.get_row_change_table_sql(binding),
                    column,
                    recovery=self.get_row_recovery(binding),
                )
            )
        else:
            value = backfill.field_backfill_sql_literal(new_field, binding)
            if value is None:
                steps.append(ColumnNullPrecondition(qualified_table, column))
            else:
                steps.append(
                    SchemaStatement(
                        backfill.get_fill_nulls_sql(self.get_row_change_table_sql(binding), column, value),
                        frozenset({SchemaEffect.BACKFILL_ROWS}),
                        recovery=self.get_row_recovery(binding),
                    )
                )
        steps.extend(
            backfill.get_not_null_plan(
                binding,
                new_field,
                column,
                self.column_definitions.get_altered_column_type(new_sql_type, False),
                column_has_db_default=old_field.has_db_default(),
            ).steps
        )
        return SchemaChangePlan(tuple(steps))

    def _get_default_change_sql(
        self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any]
    ) -> str | None:
        """The ALTER setting or dropping a column's database default.

        Args:
            binding: The model.
            old_field: The field before the change.
            new_field: The field after it.

        Returns:
            The ALTER, None when the default stays.
        """
        old_has_db_default = old_field.has_db_default()
        new_has_db_default = new_field.has_db_default()
        if old_has_db_default == new_has_db_default and (
            not new_has_db_default or old_field.db_default == new_field.db_default
        ):
            return None
        column = self.quote(new_field.source_field or new_field.model_field_name)
        if not new_has_db_default:
            changes = self.ALTER_FIELD_DROP_DEFAULT_TEMPLATE.format(column=column)
        else:
            changes = self.ALTER_FIELD_SET_DEFAULT_TEMPLATE.format(
                column=column, default=self.column_definitions.get_field_db_default_sql(new_field, binding)
            )
        return self.get_column_change_sql(binding, self.ALTER_FIELD_TEMPLATE, changes=changes)

    async def alter_field(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        field_name: str,
        candidate_referencing_models: Sequence[ModelBinding] = (),
    ) -> None:
        """Alters one field's column (or M2M through table) from its old to its new definition.

        Args:
            old_binding: The binding of the model rendered from the state the database currently matches.
            new_binding: The binding of the model rendered from the target state.
            field_name: The altered field's name.
            candidate_referencing_models: Models of the target state that may hold relations to
                `new_model` - a changed column type is carried over to their key columns.
        """
        await self.prepare_schema()
        await self.get_alter_field_plan(old_binding, new_binding, field_name, candidate_referencing_models).execute(
            self
        )

    def get_alter_field_plan(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        field_name: str,
        candidate_referencing_models: Sequence[ModelBinding] = (),
    ) -> SchemaChangePlan:
        """Compile a complete field change, including both sides of dependent relations."""
        old_field = old_binding.fields_map[field_name]
        new_field = new_binding.fields_map[field_name]
        old_relation = isinstance(old_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance))
        new_relation = isinstance(new_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance))
        if old_relation != new_relation:
            return self.get_relation_conversion_plan(old_binding, new_binding, field_name)
        if isinstance(old_field, ManyToManyFieldInstance):
            return self.many_to_many_through_tables.get_many_to_many_change_plan(new_binding, old_field, new_field)  # type: ignore[arg-type]
        if isinstance(old_field, ForeignKeyFieldInstance):
            old_source_fields = old_field.source_fields
            old_names = old_source_fields if len(old_source_fields) > 1 else (old_field.source_field or field_name,)
            new_source_fields = new_field.source_fields  # type: ignore[attr-defined]
            new_names = new_source_fields if len(new_source_fields) > 1 else (new_field.source_field or field_name,)
            pairs = [
                (old_binding.fields_map[old], new_binding.fields_map[new])
                for old, new in zip(old_names, new_names, strict=True)
            ]
            keys = self.foreign_key_rebuild
            target_changed = keys.get_relation_target_columns(old_field) != keys.get_relation_target_columns(new_field)  # type: ignore[arg-type]
            type_changed = any(
                old.get_column_type(self.client.dialect) != new.get_column_type(self.client.dialect)
                for old, new in pairs
            )
            steps: list[SchemaStep] = []
            if target_changed:
                steps.extend(keys.get_relation_target_check_plan(old_binding, old_field, new_field).steps)  # type: ignore[arg-type]
            rebuild_key = target_changed or type_changed or keys.foreign_key_changed(old_field, new_field)
            if rebuild_key:
                steps.extend(keys.get_relation_foreign_key_drop_plan(old_binding, old_field).steps)
            index_drop = keys.get_composite_index_drop_plan(new_binding, old_field, pairs)
            steps.extend(index_drop.steps)
            for old_key, new_key in pairs:
                steps.extend(self.get_alter_column_plan(new_binding, old_key, new_key).steps)
            if rebuild_key:
                steps.extend(keys.get_relation_foreign_key_restore_plan(new_binding, new_field).steps)
            steps.extend(
                keys.get_composite_index_change_plan(
                    new_binding,
                    old_field,
                    new_field,  # type: ignore[arg-type]
                    index_dropped=bool(index_drop.steps),
                ).steps
            )
            steps.extend(
                self.get_altered_column_names_plan(
                    old_binding, new_binding, field_name, rebuilt_foreign_key=field_name if rebuild_key else None
                ).steps
            )
            return SchemaChangePlan(tuple(steps))
        rewrite = self.column_backfill.get_stored_value_rewrite_plan(new_binding, old_field, new_field)
        references = []
        if candidate_referencing_models and old_field.get_column_type(
            self.client.dialect
        ) != new_field.get_column_type(self.client.dialect):
            references = self.foreign_key_rebuild.get_referencing_key_columns(
                new_binding, field_name, candidate_referencing_models
            )
        change = (
            self.foreign_key_rebuild.get_referenced_field_change_plan(new_binding, old_field, new_field, references)
            if references
            else self.get_alter_column_plan(new_binding, old_field, new_field)
        )
        return SchemaChangePlan(
            (
                *change.steps,
                *self.get_altered_column_names_plan(old_binding, new_binding, field_name).steps,
                *rewrite.steps,
            ),
            before=self.table_creation.get_definition(old_binding),
            after=self.table_creation.get_definition(new_binding),
        )

    def get_altered_column_names_plan(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        field_name: str,
        *,
        rebuilt_foreign_key: str | None = None,
    ) -> SchemaChangePlan:
        """Keep generated schema object names in sync after a physical column rename."""
        if self.RENAME_CONSTRAINT_TEMPLATE is None or old_binding.get_column_names((field_name,)) == (
            new_binding.get_column_names((field_name,))
        ):
            return SchemaChangePlan(())
        return self.generated_index_names.get_generated_index_names_change_plan(
            old_binding, new_binding, after_column_alter=True, rebuilt_foreign_key=rebuilt_foreign_key
        )

    def get_relation_conversion_plan(
        self, old_binding: ModelBinding, new_binding: ModelBinding, field_name: str
    ) -> SchemaChangePlan:
        """Turns a plain field into a forward relation over its column, or a relation back into a
        plain field: the column altered as the relation's key column - with the relation's index
        and uniqueness - and the foreign key constraint added or dropped.

        Args:
            old_binding: The binding of the model rendered from the state the database currently matches.
            new_binding: The binding of the model rendered from the target state.
            field_name: The altered field's name.

        Raises:
            ConfigurationError: One of the fields is a many-to-many relation or a relation to a
                composite key - no one column stands for it.
        """
        old_field = old_binding.fields_map[field_name]
        new_field = new_binding.fields_map[field_name]
        relation_field = old_field if isinstance(old_field, ForeignKeyFieldInstance) else new_field
        if not isinstance(relation_field, ForeignKeyFieldInstance) or len(relation_field.source_fields) > 1:
            raise ConfigurationError(
                f"Converting field {field_name!r} between a many-to-many or composite-key relation and a "
                "plain column is not supported by AlterField - remove the field and add it back instead."
            )
        relation_binding = old_binding if relation_field is old_field else new_binding
        key_field = copy(relation_binding.fields_map[relation_field.source_field or field_name])
        key_field.index = relation_field.index
        key_field.unique = relation_field.unique
        if relation_field is new_field:
            # The old column under the key field's name: a table rebuild matches the altered field
            # by it, and keeps the relation field rendering the column with its REFERENCES.
            column_field = copy(old_field)
            column_field.source_field = old_field.source_field or old_field.model_field_name
            column_field.model_field_name = key_field.model_field_name
            return SchemaChangePlan(
                (
                    *self.get_alter_column_plan(new_binding, column_field, key_field).steps,
                    *self.foreign_key_rebuild.get_relation_foreign_key_restore_plan(new_binding, new_field).steps,
                )
            )
        return SchemaChangePlan(
            (
                *self.foreign_key_rebuild.get_relation_foreign_key_drop_plan(old_binding, old_field).steps,
                *self.get_alter_column_plan(new_binding, key_field, new_field).steps,
            )
        )

    async def remove_field(self, old_binding: ModelBinding, new_binding: ModelBinding, field: Field[Any]) -> None:
        """Drops a field's column (or a many-to-many relation's through table) - its constraints and
        indexes go with it.

        Args:
            old_binding: The binding of the model rendered from the state with the field - the table as it is.
            new_binding: The binding of the model rendered from the state without it.
            field: The field, of ``old_model``.
        """
        await self.prepare_schema()
        await self.get_remove_field_plan(old_binding, new_binding, field).execute(self)

    def get_remove_field_plan(
        self, old_binding: ModelBinding, new_binding: ModelBinding, field: Field[Any]
    ) -> SchemaChangePlan:
        """Compile dependent-index and column removal before executing either."""
        if isinstance(field, ManyToManyFieldInstance):
            return self.many_to_many_through_tables.get_through_table_drop_plan(old_binding, field)
        steps: list[SchemaStep] = []
        if not self.client.features.alters_indexed_columns:
            indexes = self.index_statements.get_indexes_covering_field(
                old_binding, field, field.index and not field.pk
            )
            for index in indexes:
                steps.extend(self.get_remove_index_plan(old_binding, index).steps)
        fields = self.column_type_changes.get_columns(old_binding, field, field.model_field_name)
        for column_field in fields:
            column = old_binding.fields_db_projection.get(
                column_field.model_field_name, column_field.source_field or column_field.model_field_name
            )
            steps.append(
                SchemaStatement(
                    self.get_column_change_sql(old_binding, self.DELETE_FIELD_TEMPLATE, column=self.quote(column))
                )
            )
        return SchemaChangePlan(tuple(steps))

    async def add_index(self, binding: ModelBinding, index: Index, concurrently: bool = False) -> None:
        """Creates an index.

        Args:
            binding: The indexed model.
            index: The index.
            concurrently: Build it without blocking writes - on a dialect that can
                (``Features.supports_concurrent_indexes``); elsewhere it is built the plain way.
        """
        await self.prepare_schema()
        await self.get_add_index_plan(binding, index, concurrently=concurrently).execute(self)

    def get_add_index_plan(self, binding: ModelBinding, index: Index, concurrently: bool = False) -> SchemaChangePlan:
        """Compile ordinary index creation, allowing dialect result-checking steps."""
        sql = self.index_statements.get_index_create_sql(binding, index)
        return SchemaChangePlan((SchemaStatement(sql),) if sql else ())

    async def remove_index(self, binding: ModelBinding, index: Index, concurrently: bool = False) -> None:
        """Drops an index.

        Args:
            binding: The indexed model.
            index: The index.
            concurrently: Drop it without blocking reads and writes - on a dialect that can
                (``Features.supports_concurrent_indexes``); elsewhere it is dropped the plain way.
        """
        await self.prepare_schema()
        await self.get_remove_index_plan(binding, index, concurrently=concurrently).execute(self)

    def get_remove_index_plan(
        self, binding: ModelBinding, index: Index, concurrently: bool = False
    ) -> SchemaChangePlan:
        """Compile index removal, allowing a dialect to select its execution form."""
        return SchemaChangePlan((SchemaStatement(self.index_statements.get_index_drop_sql(binding, index)),))

    async def rename_index(self, binding: ModelBinding, old_index: Index, new_index: Index) -> None:
        """Renames an index of a model - drops and creates it where the database renames no index.

        Args:
            binding: The model.
            old_index: The index as declared before.
            new_index: The index as declared now.
        """
        await self.prepare_schema()
        await self.get_rename_index_plan(binding, old_index, new_index).execute(self)

    def get_rename_index_plan(self, binding: ModelBinding, old_index: Index, new_index: Index) -> SchemaChangePlan:
        """Prepare renaming or rebuilding an index without changing it yet."""
        old_name = self.index_statements.index_name_for_model(binding, old_index)
        new_name = self.index_statements.index_name_for_model(binding, new_index)
        if old_name == new_name:
            return SchemaChangePlan(())
        sql = self.index_statements.get_rename_sql(old_name, new_name, binding.schema)
        if sql is not None:
            return SchemaChangePlan((SchemaStatement(sql),))
        return SchemaChangePlan(
            (
                *self.get_remove_index_plan(binding, old_index).steps,
                *self.get_add_index_plan(binding, new_index).steps,
            )
        )

    async def add_constraint(
        self,
        binding: ModelBinding,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        """Adds a constraint to a model's table - a uniqueness as a unique index where the database keeps no
        unique constraint of its own form.

        Args:
            binding: The model.
            constraint: The constraint.
        """
        await self.prepare_schema()
        await self.get_add_constraint_plan(binding, constraint).execute(self)

    def get_add_constraint_plan(
        self,
        binding: ModelBinding,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> SchemaChangePlan:
        """Compile constraint creation before any enclosing schema operation starts."""
        clause: str | None
        if isinstance(constraint, ExclusionConstraint):
            clause = self.constraint_statements.exclusion_constraint_sql(binding, constraint)
        elif isinstance(constraint, ForeignKeyConstraint):
            clause = self.table_creation.get_foreign_key_constraint_clause(constraint)
        elif isinstance(constraint, CheckConstraint):
            clause = self.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.quote(constraint.name),
                check=ConstraintCondition.get_sql(constraint.check, binding, self.client),
            )
        else:
            self.constraint_statements.check_unique_constraint_supported(constraint)
            clause = self.constraint_statements.get_unique_constraint_clause(binding, constraint)
            if clause is None:
                return SchemaChangePlan(
                    (SchemaStatement(self.constraint_statements.get_unique_index_sql(binding, constraint)),)
                )
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.ADD_CONSTRAINT_TEMPLATE.format(
                        table=self.get_storage_table_sql(binding),
                        constraint=clause,
                    )
                ),
            )
        )

    async def remove_constraint(
        self,
        binding: ModelBinding,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        """Drops a constraint of a model's table - a uniqueness kept as a unique index drops the index.

        Args:
            binding: The model.
            constraint: The constraint.
        """
        await self.prepare_schema()
        await self.get_remove_constraint_plan(binding, constraint).execute(self)

    def get_remove_constraint_plan(
        self,
        binding: ModelBinding,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> SchemaChangePlan:
        """Prepare removal using the declared constraint or its backing index."""
        if isinstance(constraint, (CheckConstraint, ExclusionConstraint, ForeignKeyConstraint)):
            name = constraint.name
        elif constraint.condition:
            return SchemaChangePlan(
                (
                    SchemaStatement(
                        self.DROP_INDEX_TEMPLATE.format(
                            name=self.qualify_table_name(
                                self.constraint_names.constraint_name_for_model(binding, constraint),
                                binding.schema,
                            ),
                            table=self.get_storage_table_sql(binding),
                        )
                    ),
                )
            )
        else:
            name = self.constraint_names.constraint_name_for_model(binding, constraint)
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.DELETE_CONSTRAINT_TEMPLATE.format(
                        table=self.get_storage_table_sql(binding),
                        name=self.quote(name),
                    )
                ),
            )
        )

    async def rename_constraint(
        self,
        binding: ModelBinding,
        old_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        new_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    ) -> None:
        """Renames a constraint of a model's table - by dropping and adding it where the database renames
        none, or the type of constraint changed.

        Args:
            binding: The model.
            old_constraint: The constraint as declared before.
            new_constraint: The constraint as declared now.
        """
        await self.prepare_schema()
        await self.get_rename_constraint_plan(binding, old_constraint, new_constraint).execute(self)

    def get_rename_constraint_plan(
        self,
        binding: ModelBinding,
        old_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        new_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    ) -> SchemaChangePlan:
        """Compile the whole rename or replacement before removing the old declaration."""
        if isinstance(old_constraint, UniqueConstraint) and old_constraint.condition:
            return self.constraint_statements.get_partial_unique_rename_plan(binding, old_constraint, new_constraint)
        # For CheckConstraint/ExclusionConstraint or any named constraint, use names directly
        if isinstance(old_constraint, (CheckConstraint, ExclusionConstraint)):
            if type(new_constraint) is not type(old_constraint):
                raise TypeError(f"Cannot rename {type(old_constraint).__name__} to {type(new_constraint).__name__}")
            old_name = old_constraint.name
            new_name = new_constraint.name
        else:
            if not isinstance(new_constraint, UniqueConstraint):
                raise TypeError(f"Cannot rename UniqueConstraint to {type(new_constraint).__name__}")
            old_name = self.constraint_names.constraint_name_for_model(binding, old_constraint)
            new_name = self.constraint_names.constraint_name_for_model(binding, new_constraint)
        if old_name == new_name:
            return SchemaChangePlan(())
        if self.RENAME_CONSTRAINT_TEMPLATE:
            return SchemaChangePlan(
                (
                    SchemaStatement(
                        self.RENAME_CONSTRAINT_TEMPLATE.format(
                            table=self.get_storage_table_sql(binding),
                            old_name=self.quote(old_name),
                            new_name=self.quote(new_name),  # type: ignore[arg-type]
                        )
                    ),
                )
            )
        return SchemaChangePlan(
            (
                *self.get_remove_constraint_plan(binding, old_constraint).steps,
                *self.get_add_constraint_plan(binding, new_constraint).steps,
            )
        )

    def raise_if_unsupported(self, feature: str, objects: str) -> None:
        """Refuses a type of database object the dialect doesn't have - before any SQL is sent.

        Args:
            feature: The ``Features`` flag of the type.
            objects: The type in words, plural - for the message.

        Raises:
            UnSupportedError: The flag is off.
        """
        if not getattr(self.client.features, feature):
            raise UnSupportedError(f"{objects.capitalize()} are not supported on {self.client.dialect}")

    async def run_sqls(self, statements: Iterable[str]) -> None:
        """Runs statements one by one."""
        for statement in statements:
            await self.run_sql(statement)
