from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import Any, ClassVar, cast

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.schema_objects.view import View
from hare.dialects.base.client.database_client import DatabaseClient
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
from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.indexes.generated_index_names import GeneratedIndexNames
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
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
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral


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

    DELETE_CONSTRAINT_TEMPLATE = "ALTER TABLE {table} DROP CONSTRAINT {name}"
    ADD_CONSTRAINT_TEMPLATE = "ALTER TABLE {table} ADD {constraint}"
    #: An ``ADD_CONSTRAINT_TEMPLATE`` statement ({statement}) doing nothing when the constraint
    #: exists; None for a database with no such statement - the constraint is added as it is.
    SAFE_ADD_CONSTRAINT_TEMPLATE: str | None = None
    # DROP INDEX/ALTER INDEX name no table: {name}/{old_name} are passed schema-qualified,
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

    async def run_sql(self, sql: str) -> None:
        """Runs a DDL statement - a dialect's editor runs it its own way where it needs to.

        If ``collect_sql`` is True, append the SQL to ``collected_sql`` instead
        of executing it.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        ProtectConstraintDeferral.forget_constraint_names()
        self.run_statement_count += 1
        await self.client.execute_built_script(sql)

    async def run_raw_sql(self, sql: str) -> None:
        """Executes SQL a migration's author wrote (``RunSQL``) as it is written.

        If ``collect_sql`` is True, append the SQL to ``collected_sql`` instead of executing it.

        Args:
            sql: The SQL.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        ProtectConstraintDeferral.forget_constraint_names()
        self.run_statement_count += 1
        await self.client.execute_script(sql)

    async def run_parameterized_sql(self, sql: str, parameters: Sequence[Any]) -> int:
        """Runs a statement of a migration that changes rows, with its parameters - a backfill's batch, a
        ``RunSQL`` pair - counted as a statement run, as ``run_sql()`` counts its own.

        Args:
            sql: The statement.
            parameters: Its parameters.

        Returns:
            The number of rows it changed.
        """
        self.run_statement_count += 1
        rowcount, _ = await self.client.execute(sql, list(parameters))
        return rowcount or 0

    @asynccontextmanager
    async def constraint_checking_disabled(self) -> AsyncGenerator[None]:
        """No-op by default - only an editor rebuilding tables to change their columns needs this; one
        whose ALTER TABLE changes everything in place never drops and recreates a table."""
        yield

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

    def qualify_object_name(self, model: type[Model], name: str) -> str:
        """The quoted name of an object a model declares, in the model's schema."""
        return self.qualify_table_name(name, model._meta.schema)

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

    async def create_model(self, model: type[Model]) -> None:
        """Creates a model's table with its indexes, triggers and constraints, and its automatic
        through tables.

        Args:
            model: The model.
        """
        model_sql_data = self.table_creation.get_model_sql_data(model)
        # Built before any statement runs - a dialect without an object the model declares
        # refuses it before the table is created.
        before_table_sqls = self.table_creation.get_schema_objects_before_table_sqls(model)
        after_table_sqls = self.table_creation.get_schema_objects_after_table_sqls(model)
        await self.run_sqls(before_table_sqls)
        await self.run_sql("\n".join([model_sql_data.table_sql, *model_sql_data.many_to_many_tables_sql]))
        for trigger in model._meta.triggers:
            await self.trigger_statements.add_trigger(model, trigger)
        for constraint_sql in model_sql_data.constraint_sqls:
            await self.run_sql(constraint_sql)
        await self.run_sqls(after_table_sqls)

    async def rename_table(self, model: type[Model], old_name: str, new_name: str) -> None:
        """Renames a model's table in its schema.

        Args:
            model: The model.
            old_name: The table's name.
            new_name: Its new name.
        """
        if old_name == new_name:
            return
        schema = model._meta.schema
        await self.run_sql(
            self.RENAME_TABLE_TEMPLATE.format(
                old_table=self.qualify_table_name(old_name, schema),
                new_table=self.quote(new_name),
            )
        )

    async def delete_model(self, model: type[Model]) -> None:
        """Drops a model's table, with the views, dictionaries and through tables over it, and the table its
        rows are stored in when it is another.

        Args:
            model: The model.
        """
        schema = model._meta.schema
        await self.materialized_views.drop_model_materialized_views(model)
        await self.dictionaries.drop_model_dictionaries(model)
        await self.views.drop_model_views(model)
        await self.many_to_many_through_tables.drop_model_through_tables(model)
        await self.run_sql(
            self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(model._meta.db_table, schema))
        )
        table_options = model._meta.get_table_options(self.client.dialect)
        if table_options is not None:
            storage_table_name = table_options.get_storage_table_name(model._meta.db_table)
            if storage_table_name != model._meta.db_table:
                # The rows are stored in a table of their own, behind the model's.
                await self.run_sql(
                    self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(storage_table_name, schema))
                )

    async def _add_composite_foreign_key_field(
        self, model: type[Model], field: ForeignKeyFieldInstance[Model], *, foreign_key_not_valid: bool = False
    ) -> None:
        """Adds a relation to a composite key to an existing table: one ``ADD COLUMN`` per key column,
        then one table-level ``FOREIGN KEY`` constraint - unvalidated with ``foreign_key_not_valid``.
        """
        for key_field_name in field.source_fields:
            await self.run_sql(
                self.ADD_FIELD_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    definition=self.column_definitions.get_column_definition(model, key_field_name),
                )
            )
        if self.foreign_key_rebuild.creates_foreign_key(field):
            constraint = self.table_creation.get_composite_foreign_key_constraint(model, field)
            if foreign_key_not_valid:
                await self.foreign_key_rebuild.add_foreign_key_constraint_not_valid(model, constraint)
            else:
                await self.add_constraint(model, constraint)
        if isinstance(field, OneToOneFieldInstance):
            # The relation's own uniqueness, over its key columns.
            await self.add_constraint(model, UniqueConstraint(fields=tuple(field.source_fields)))
        await self.add_field_indexes(model, field)

    async def add_field(
        self, old_model: type[Model], new_model: type[Model], field_name: str, *, foreign_key_not_valid: bool = False
    ) -> None:
        """Adds a field's column (or a many-to-many relation's through table) to an existing table, then its
        uniqueness and index.

        Args:
            old_model: The model rendered from the state without the field - the table as it is.
            new_model: The model rendered from the state with the field.
            field_name: The field's name.
            foreign_key_not_valid: For a relation with a database constraint: add the FOREIGN KEY
                without checking the existing rows (``add_foreign_key_constraint_not_valid()``) -
                a later ``validate_constraint()`` checks them.
        """
        model = new_model
        field = model._meta.fields_map[field_name]
        if isinstance(field, ManyToManyFieldInstance):
            table_string = self.table_creation.get_many_to_many_table_definition(model, field)
            if table_string:
                await self.run_sql(table_string)
            return

        needs_backfill = False
        unvalidated_foreign_key = None
        if isinstance(field, ForeignKeyFieldInstance):
            if len(field.source_fields) > 1:
                await self._add_composite_foreign_key_field(model, field, foreign_key_not_valid=foreign_key_not_valid)
                return
            key_field_name = field.source_field or field_name
            if foreign_key_not_valid and self.foreign_key_rebuild.creates_foreign_key(field):
                field_definition = self.column_definitions.get_column_definition(
                    model, key_field_name, with_reference=False
                )
                unvalidated_foreign_key = self.table_creation.get_single_column_foreign_key_constraint(
                    model, key_field_name
                )
            else:
                field_definition = self.column_definitions.get_column_definition(model, key_field_name)
            column_field_name = key_field_name
        else:
            # Added nullable first, filled, then made NOT NULL - a NOT NULL column with only a
            # Python default can't be added to a table that already holds rows in one step.
            needs_backfill = self.column_backfill.needs_added_column_backfill(field)
            field_definition = self.column_definitions.get_column_definition(
                model, field_name, nullable=field.null or needs_backfill
            )
            column_field_name = field_name
        db_field = model._meta.fields_db_projection[column_field_name]

        await self.run_sql(
            self.ADD_FIELD_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                definition=field_definition,
            )
        )
        # The column's comment where the database sets it by a statement of its own.
        for comment_sql in self.table_comments.get_model_comment_statements(model, (column_field_name,)):
            await self.run_sql(comment_sql)
        rebuilt = False
        if needs_backfill:
            rebuilt = await self.column_backfill.backfill_added_column(model, field, db_field)
        elif not isinstance(field, ForeignKeyFieldInstance):
            await self.column_backfill.keep_added_column_default(model, field, db_field)
        if unvalidated_foreign_key is not None:
            await self.foreign_key_rebuild.add_foreign_key_constraint_not_valid(model, unvalidated_foreign_key)
        if not rebuilt:
            # After the rows are filled - a rebuilt table has its uniqueness and indexes already.
            for constraint in self.table_creation.get_field_unique_constraints(model):
                if constraint.fields == (db_field,):
                    await self.add_constraint(model, constraint)
            await self.add_field_indexes(model, field)

    async def add_field_indexes(self, model: type[Model], field: Field[Any]) -> None:
        """Creates the indexes a just-added field's own ``db_index=True`` implies.

        Args:
            model: The model rendered from the state with the field.
            field: The added field.
        """
        for index in self.index_statements.get_field_indexes(model, field):
            await self.add_index(model, index)

    async def _alter_generated_field(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> bool:
        if old_field.pk or new_field.pk:
            return False

        old_generated_sql = old_field.get_generated_sql(self.client.dialect) if old_field.generated else None
        new_generated_sql = new_field.get_generated_sql(self.client.dialect) if new_field.generated else None
        # The generated SQL doesn't show output_field's type - the column type is compared too, so a
        # change of output_field alone is seen.
        old_sql_type = old_field.get_column_type(self.client.dialect) if old_field.generated else None
        new_sql_type = new_field.get_column_type(self.client.dialect) if new_field.generated else None
        if old_generated_sql == new_generated_sql and old_sql_type == new_sql_type:
            return False
        if old_field.generated or new_field.generated:
            raise ConfigurationError(
                f"Modifying generated fields is not supported - the field {new_field} "
                "must be removed and re-added with the new definition."
            )
        return False

    async def alter_column(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        """Changes a field's column in place: its name, type, nullability, indexes, uniqueness,
        comment and default.

        Args:
            model: The model, as it is after the change.
            old_field: The field before the change.
            new_field: The field after it.

        Raises:
            ConfigurationError: A generated field changes.
        """
        actions: list[str] = []
        dependent_generated_fields: list[Field[Any]] = []
        old_db_field = old_field.source_field or old_field.model_field_name
        new_db_field = new_field.source_field or new_field.model_field_name
        qualified_table = self.qualify_table_name(model._meta.db_table, model._meta.schema)
        if await self._alter_generated_field(model, old_field, new_field):
            return
        if not self.collect_sql and old_field.get_column_type(self.client.dialect) != new_field.get_column_type(
            self.client.dialect
        ):
            # The data is checked before anything changes - a database without transactional DDL
            # would keep the steps made before a refusal.
            await self.column_conversion_check.check_alter_field_conversion(
                qualified_table, old_field, new_field, self.quote(old_db_field)
            )
            if self.client.features.truncates_values_on_type_change:
                # A cast to the new type truncates a too long string and rounds a too precise
                # decimal without an error.
                await self.column_narrowing_check.check_alter_field_narrowing_data_loss(
                    qualified_table, old_field, new_field, self.quote(old_db_field)
                )
        if await self.table_rebuild.remake_for_column_change(model, old_field, new_field):
            return
        if old_db_field != new_db_field:
            # The rename runs first, at once - everything below refers to the column by its new
            # name.
            await self.run_sql(
                self.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table,
                    old_column=self.quote(old_db_field),
                    new_column=self.quote(new_db_field),
                )
            )
        old_sql_type = old_field.get_column_type(self.client.dialect)
        new_sql_type = new_field.get_column_type(self.client.dialect)
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        held_indexes: list[Index] = []
        if not self.client.features.alters_indexed_columns and (
            old_sql_type != new_sql_type or old_field.null != new_field.null
        ):
            # The indexes covering the column are dropped for the change and created again after it.
            held_indexes = self.index_statements.get_indexes_covering_field(
                model, new_field, old_indexed, adds_field_index=new_indexed and not old_indexed
            )
            for index in held_indexes:
                # Named after the column as it was - a renamed column's indexes keep their old names.
                await self.remove_index(
                    model,
                    self.index_statements.get_index_before_column_rename(model, index, old_db_field, new_db_field),
                )
        # Before the column change - a unique constraint left on it would refuse its backfill.
        await self._remove_column_index_and_uniqueness(model, old_field, new_field, holds_indexes=bool(held_indexes))
        if old_sql_type != new_sql_type:
            if self.DROPS_DEFAULT_BEFORE_TYPE_CHANGE and old_field.has_db_default():
                await self.run_sql(
                    self.ALTER_FIELD_TEMPLATE.format(
                        table=qualified_table,
                        changes=self.ALTER_FIELD_DROP_DEFAULT_TEMPLATE.format(column=self.quote(new_db_field)),
                    )
                )
                # The column has no default now - the new field's is set below.
                old_field = copy(old_field)
                old_field.db_default = DB_DEFAULT_NOT_SET
            dependent_generated_fields = await self._alter_column_type(
                model, qualified_table, old_field, new_field, new_sql_type
            )
        if old_field.null != new_field.null:
            actions.append(
                await self._get_nullability_change_sql(model, qualified_table, old_field, new_field, new_sql_type)
            )
        if old_field.description != new_field.description:
            await self.table_comments.alter_column_comment(model, old_field, new_field)
        default_change_sql = self._get_default_change_sql(model, qualified_table, old_field, new_field)
        if default_change_sql is not None:
            actions.append(default_change_sql)

        if actions:
            result_query = ";\n".join(actions)
            await self.run_sql(result_query)
        # After the column change - a database may refuse to alter a column an index already covers.
        await self._add_column_index_and_uniqueness(model, old_field, new_field)

        for position, index in enumerate(held_indexes):
            # The column's own index - held first - comes back only while the field still has one.
            if position or not old_indexed or new_indexed:
                await self.add_index(model, index)

        for generated_field in dependent_generated_fields:
            await self.add_field(model, model, generated_field.model_field_name)

    async def _remove_column_index_and_uniqueness(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any], *, holds_indexes: bool
    ) -> None:
        """Drops the column's own index and its unique constraint the change takes away - and renames the
        ones it keeps after a renamed column, their generated names made from the column.

        Args:
            model: The model, the column already renamed.
            old_field: The field before the change.
            new_field: The field after it.
            holds_indexes: Whether the column's indexes were dropped for the change - its own index
                then isn't there to drop or rename.
        """
        old_db_field = old_field.source_field or old_field.model_field_name
        new_db_field = new_field.source_field or new_field.model_field_name
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if old_indexed and not holds_indexes:
            field_index = Index(fields=(new_db_field,))
            old_field_index = self.index_statements.get_index_before_column_rename(
                model, field_index, old_db_field, new_db_field
            )
            if not new_indexed:
                # A Meta.indexes entry over the column under the same name keeps the index.
                if not self.index_statements.is_declared_by_another_entry(
                    model, field_index, model_declares_index=False
                ):
                    await self.remove_index(model, old_field_index)
            elif old_field_index is not field_index:
                await self.rename_index(model, old_field_index, field_index)
        if old_field.unique and not new_field.unique:
            await self.remove_constraint(model, UniqueConstraint(fields=(old_db_field,)))
        elif old_field.unique and not old_field.pk and not new_field.pk and old_db_field != new_db_field:
            # A primary key's uniqueness is the key itself - it has no unique constraint to rename.
            await self.rename_constraint(
                model, UniqueConstraint(fields=(old_db_field,)), UniqueConstraint(fields=(new_db_field,))
            )

    async def _add_column_index_and_uniqueness(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any]
    ) -> None:
        """Adds the column's own index and its unique constraint the change brings.

        Args:
            model: The model.
            old_field: The field before the change.
            new_field: The field after it.
        """
        new_db_field = new_field.source_field or new_field.model_field_name
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if new_indexed and not old_indexed:
            field_index = Index(fields=(new_db_field,))
            # A Meta.indexes entry over the column under the same name made the index already.
            if not self.index_statements.is_declared_by_another_entry(model, field_index, model_declares_index=True):
                await self.add_index(model, field_index)
        if new_field.unique and not old_field.unique:
            await self.add_constraint(model, UniqueConstraint(fields=(new_db_field,)))

    async def _alter_column_type(
        self,
        model: type[Model],
        qualified_table: str,
        old_field: Field[Any],
        new_field: Field[Any],
        new_sql_type: str,
    ) -> list[Field[Any]]:
        """Changes a column's type at once - a backfill after it writes a value of the new type.

        Args:
            model: The model.
            qualified_table: The model's quoted table.
            old_field: The field before the change.
            new_field: The field after it.
            new_sql_type: The column's new type.

        Returns:
            The generated fields reading the column - dropped for the change, created again by the
            caller after it.
        """
        new_db_field = new_field.source_field or new_field.model_field_name
        # A database may refuse to change the type of a column a generated column depends on - the
        # dependent generated columns are dropped and created again afterwards.
        dependent_generated_fields = self.column_type_changes.get_generated_fields_depending_on_column(
            model, new_db_field
        )
        for generated_field in dependent_generated_fields:
            await self.remove_field(model, model, generated_field)
        # The column keeps its nullability here - a NOT NULL change after it backfills the NULL rows first.
        changes = self.column_type_changes.get_type_change_sql(
            old_field,
            new_field,
            self.quote(new_db_field),
            self.column_definitions.get_altered_column_type(new_sql_type, old_field.null),
        )
        await self.run_sql(self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes))
        return dependent_generated_fields

    async def _get_nullability_change_sql(
        self,
        model: type[Model],
        qualified_table: str,
        old_field: Field[Any],
        new_field: Field[Any],
        new_sql_type: str,
    ) -> str:
        """The ALTER making a column nullable or not. A column made NOT NULL gets the field's
        default in its NULL rows first - written at once.

        Args:
            model: The model.
            qualified_table: The model's quoted table.
            old_field: The field before the change.
            new_field: The field after the change.
            new_sql_type: The column's type.

        Returns:
            The ALTER.
        """
        new_db_field = new_field.source_field or new_field.model_field_name
        if new_field.null:
            changes = self.ALTER_FIELD_NULL_TEMPLATE.format(
                column=self.quote(new_db_field),
                sql_type=self.column_definitions.get_altered_column_type(new_sql_type, True),
            )
        else:
            backfill_value = self.column_backfill.field_backfill_sql_literal(new_field, model)
            if backfill_value is not None:
                await self.run_sql(
                    self.UPDATE_ROWS_TEMPLATE.format(
                        table=qualified_table,
                        assignments=f"{self.quote(new_db_field)} = {backfill_value}",
                        condition=f"{self.quote(new_db_field)} IS NULL",
                    )
                )
            elif not self.collect_sql:
                await self.column_narrowing_check.check_not_null_change(qualified_table, self.quote(new_db_field))
            return self.column_backfill.get_not_null_sql(
                model,
                new_field,
                qualified_table,
                self.quote(new_db_field),
                self.column_definitions.get_altered_column_type(new_sql_type, False),
                column_has_db_default=old_field.has_db_default(),
            )
        return self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes)

    def _get_default_change_sql(
        self, model: type[Model], qualified_table: str, old_field: Field[Any], new_field: Field[Any]
    ) -> str | None:
        """The ALTER setting or dropping a column's database default.

        Args:
            model: The model.
            qualified_table: The model's quoted table.
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
                column=column, default=self.column_definitions.get_field_db_default_sql(new_field, model)
            )
        return self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes)

    async def alter_field(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_name: str,
        candidate_referencing_models: Sequence[type[Model]] = (),
    ) -> None:
        """Alters one field's column (or M2M through table) from its old to its new definition.

        Args:
            old_model: The model rendered from the state the database currently matches.
            new_model: The model rendered from the target state.
            field_name: The altered field's name.
            candidate_referencing_models: Models of the target state that may hold relations to
                `new_model` - a changed column type is carried over to their key columns.
        """
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]

        old_is_relation = isinstance(old_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance))
        new_is_relation = isinstance(new_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance))
        if old_is_relation != new_is_relation:
            await self._alter_column_into_or_out_of_relation(old_model, new_model, field_name)
            return

        if isinstance(old_field, ManyToManyFieldInstance):
            new_field = cast("ManyToManyFieldInstance[Model]", new_field)
            await self.many_to_many_through_tables.alter_many_to_many_field(new_model, old_field, new_field)
            return

        if isinstance(old_field, ForeignKeyFieldInstance):
            new_field = cast("ForeignKeyFieldInstance[Model]", new_field)
            old_source = old_field.source_field or field_name
            new_source = new_field.source_field or field_name
            # on_delete and db_constraint live on the relation field - compared here, before the key
            # columns take its place below. The column still has its old name at this point.
            # One key column per component of a composite target - each is altered on its own.
            old_key_names = old_field.source_fields if len(old_field.source_fields) > 1 else (old_source,)
            new_key_names = new_field.source_fields if len(new_field.source_fields) > 1 else (new_source,)
            key_field_pairs = [
                (old_model._meta.fields_map[old_key_name], new_model._meta.fields_map[new_key_name])
                for old_key_name, new_key_name in zip(old_key_names, new_key_names, strict=True)
            ]
            old_target_columns = self.foreign_key_rebuild.get_relation_target_columns(old_field)
            target_changed = old_target_columns != self.foreign_key_rebuild.get_relation_target_columns(new_field)
            key_type_changed = any(
                old_key_field.get_column_type(self.client.dialect)
                != new_key_field.get_column_type(self.client.dialect)
                for old_key_field, new_key_field in key_field_pairs
            )
            if target_changed or key_type_changed:
                # The constraint can't survive the key column switching to another column's
                # values/type - dropped first, rebuilt against the new target once it has.
                if target_changed:
                    await self.foreign_key_rebuild.raise_if_relation_values_need_remapping(
                        old_model, old_field, new_field
                    )
                await self.foreign_key_rebuild.drop_relation_foreign_keys(old_model, old_field)
                index_dropped = await self.foreign_key_rebuild.drop_composite_relation_index_for_key_change(
                    new_model, old_field, key_field_pairs
                )
                for old_key_field, new_key_field in key_field_pairs:
                    await self.alter_column(new_model, old_key_field, new_key_field)
                await self.foreign_key_rebuild.restore_relation_foreign_keys(new_model, new_field)
                await self.foreign_key_rebuild.alter_composite_relation_index(
                    new_model, old_field, new_field, index_dropped=index_dropped
                )
                return
            if self.foreign_key_rebuild.foreign_key_changed(old_field, new_field):
                await self.foreign_key_rebuild.alter_foreign_key_on_delete(new_model, old_source, old_field, new_field)
            index_dropped = await self.foreign_key_rebuild.drop_composite_relation_index_for_key_change(
                new_model, old_field, key_field_pairs
            )
            for old_key_field, new_key_field in key_field_pairs:
                await self.alter_column(new_model, old_key_field, new_key_field)
            await self.foreign_key_rebuild.alter_composite_relation_index(
                new_model, old_field, new_field, index_dropped=index_dropped
            )
            return

        referencing_key_columns: list[ReferencingKeyColumns] = []
        old_sql_type = old_field.get_column_type(self.client.dialect)
        if candidate_referencing_models and old_sql_type != new_field.get_column_type(self.client.dialect):
            referencing_key_columns = self.foreign_key_rebuild.get_referencing_key_columns(
                new_model, field_name, candidate_referencing_models
            )
        if referencing_key_columns:
            await self.foreign_key_rebuild.alter_referenced_field(
                new_model, old_field, new_field, referencing_key_columns
            )
        else:
            await self.alter_column(new_model, old_field, new_field)
        await self.column_backfill.rewrite_stored_values(new_model, old_field, new_field)

    async def _alter_column_into_or_out_of_relation(
        self, old_model: type[Model], new_model: type[Model], field_name: str
    ) -> None:
        """Turns a plain field into a forward relation over its column, or a relation back into a
        plain field: the column altered as the relation's key column - with the relation's index
        and uniqueness - and the foreign key constraint added or dropped.

        Args:
            old_model: The model rendered from the state the database currently matches.
            new_model: The model rendered from the target state.
            field_name: The altered field's name.

        Raises:
            ConfigurationError: One of the fields is a many-to-many relation or a relation to a
                composite key - no one column stands for it.
        """
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        relation_field = old_field if isinstance(old_field, ForeignKeyFieldInstance) else new_field
        if not isinstance(relation_field, ForeignKeyFieldInstance) or len(relation_field.source_fields) > 1:
            raise ConfigurationError(
                f"Converting field {field_name!r} between a many-to-many or composite-key relation and a "
                "plain column is not supported by AlterField - remove the field and add it back instead."
            )
        relation_model = old_model if relation_field is old_field else new_model
        key_field = copy(relation_model._meta.fields_map[relation_field.source_field or field_name])
        key_field.index = relation_field.index
        key_field.unique = relation_field.unique
        if relation_field is new_field:
            # The old column under the key field's name: a table rebuild matches the altered field
            # by it, and keeps the relation field rendering the column with its REFERENCES.
            column_field = copy(old_field)
            column_field.source_field = old_field.source_field or old_field.model_field_name
            column_field.model_field_name = key_field.model_field_name
            await self.alter_column(new_model, column_field, key_field)
            await self.foreign_key_rebuild.restore_relation_foreign_keys(new_model, new_field)
            return
        await self.foreign_key_rebuild.drop_relation_foreign_keys(old_model, old_field)
        await self.alter_column(new_model, key_field, new_field)

    async def remove_field(self, old_model: type[Model], new_model: type[Model], field: Field[Any]) -> None:
        """Drops a field's column (or a many-to-many relation's through table) - its constraints and
        indexes go with it.

        Args:
            old_model: The model rendered from the state with the field - the table as it is.
            new_model: The model rendered from the state without it.
            field: The field, of ``old_model``.
        """
        model = old_model
        if isinstance(field, ManyToManyFieldInstance):
            await self.many_to_many_through_tables.drop_through_table(model, field)
            return

        if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
            # Composite target: every key column is dropped - the database drops the constraint with
            # its columns.
            for source_field_name in field.source_fields:
                shadow_field = model._meta.fields_map[source_field_name]
                db_field = model._meta.fields_db_projection.get(
                    shadow_field.model_field_name, shadow_field.source_field or shadow_field.model_field_name
                )
                await self.run_sql(
                    self.DELETE_FIELD_TEMPLATE.format(
                        table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                        column=self.quote(db_field),
                    )
                )
            return

        if not self.client.features.alters_indexed_columns:
            # The indexes covering the column go first - the database drops no column they cover: the
            # field's own index by its flag, and the Meta.indexes entries keying on it.
            for index in self.index_statements.get_indexes_covering_field(model, field, field.index and not field.pk):
                await self.remove_index(model, index)
        if isinstance(field, ForeignKeyFieldInstance):
            source_field = field.source_field or field.model_field_name
            field = model._meta.fields_map[source_field]
            # The column's own FK, CHECK, UNIQUE and index are dropped with it.
        db_field = model._meta.fields_db_projection.get(
            field.model_field_name, field.source_field or field.model_field_name
        )
        await self.run_sql(
            self.DELETE_FIELD_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                column=self.quote(db_field),
            )
        )

    async def add_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Creates an index.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Build it without blocking writes - on a dialect that can
                (``Features.supports_concurrent_indexes``); elsewhere it is built the plain way.
        """
        index_sql = self.index_statements.get_index_create_sql(model, index)
        if index_sql:
            await self.run_sql(index_sql)

    async def remove_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Drops an index.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Drop it without blocking reads and writes - on a dialect that can
                (``Features.supports_concurrent_indexes``); elsewhere it is dropped the plain way.
        """
        await self.run_sql(self.index_statements.get_index_drop_sql(model, index))

    async def rename_index(self, model: type[Model], old_index: Index, new_index: Index) -> None:
        """Renames an index of a model - drops and creates it where the database renames no index.

        Args:
            model: The model.
            old_index: The index as declared before.
            new_index: The index as declared now.
        """
        old_name = self.index_statements.index_name_for_model(model, old_index)
        new_name = self.index_statements.index_name_for_model(model, new_index)
        if old_name == new_name:
            return
        if self.RENAME_INDEX_TEMPLATE:
            await self.run_sql(
                self.RENAME_INDEX_TEMPLATE.format(
                    old_name=self.qualify_table_name(old_name, model._meta.schema),
                    new_name=self.quote(new_name),
                )
            )
            return
        await self.remove_index(model, old_index)
        await self.add_index(model, new_index)

    async def add_constraint(
        self,
        model: type[Model],
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        """Adds a constraint to a model's table - a uniqueness as a unique index where the database keeps no
        unique constraint of its own form.

        Args:
            model: The model.
            constraint: The constraint.
        """
        if isinstance(constraint, ExclusionConstraint):
            await self.run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    constraint=self.constraint_statements.exclusion_constraint_sql(model, constraint),
                )
            )
            return
        if isinstance(constraint, ForeignKeyConstraint):
            constraint_sql = self.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.quote(constraint.name),
                fields=", ".join(self.quote(field_name) for field_name in constraint.fields),
                table=constraint.to_table,
                to_fields=", ".join(self.quote(field_name) for field_name in constraint.to_fields),
                on_delete=constraint.on_delete,
            )
            await self.run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    constraint=constraint_sql,
                )
            )
            return
        if isinstance(constraint, CheckConstraint):
            constraint_sql = self.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.quote(constraint.name),
                check=ConstraintCondition.get_sql(constraint.check, model, self.client),
            )
            await self.run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    constraint=constraint_sql,
                )
            )
            return
        self.constraint_statements.check_unique_constraint_supported(constraint)
        unique_clause = self.constraint_statements.get_unique_constraint_clause(model, constraint)
        if unique_clause is None:
            await self.run_sql(self.constraint_statements.get_unique_index_sql(model, constraint))
            return
        await self.run_sql(
            self.ADD_CONSTRAINT_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=unique_clause,
            )
        )

    async def remove_constraint(
        self,
        model: type[Model],
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        """Drops a constraint of a model's table - a uniqueness kept as a unique index drops the index.

        Args:
            model: The model.
            constraint: The constraint.
        """
        if isinstance(constraint, (CheckConstraint, ExclusionConstraint, ForeignKeyConstraint)):
            await self.run_sql(
                self.DELETE_CONSTRAINT_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    name=self.quote(constraint.name),
                )
            )
            return
        if constraint.condition:
            # A partial unique constraint is an index - see add_constraint().
            await self.run_sql(
                self.DROP_INDEX_TEMPLATE.format(
                    name=self.qualify_table_name(
                        self.constraint_names.get_partial_unique_index_name(model, constraint), model._meta.schema
                    ),
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                )
            )
            return
        constraint_name = self.constraint_names.constraint_name_for_model(
            model,
            UniqueConstraint(
                fields=tuple(self.constraint_names.get_fields_to_columns(model, constraint.fields)),
                name=constraint.name,
            ),
        )
        await self.run_sql(
            self.DELETE_CONSTRAINT_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                name=self.quote(constraint_name),
            )
        )

    async def rename_constraint(
        self,
        model: type[Model],
        old_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        new_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    ) -> None:
        """Renames a constraint of a model's table - by dropping and adding it where the database renames
        none, or the type of constraint changed.

        Args:
            model: The model.
            old_constraint: The constraint as declared before.
            new_constraint: The constraint as declared now.
        """
        if isinstance(old_constraint, UniqueConstraint) and old_constraint.condition:
            await self.constraint_statements.rename_partial_unique_index(model, old_constraint, new_constraint)
            return
        # For CheckConstraint/ExclusionConstraint or any named constraint, use names directly
        if isinstance(old_constraint, (CheckConstraint, ExclusionConstraint)):
            if type(new_constraint) is not type(old_constraint):
                raise TypeError(f"Cannot rename {type(old_constraint).__name__} to {type(new_constraint).__name__}")
            old_name = old_constraint.name
            new_name = cast("CheckConstraint | ExclusionConstraint", new_constraint).name
        else:
            if not isinstance(new_constraint, UniqueConstraint):
                raise TypeError(f"Cannot rename UniqueConstraint to {type(new_constraint).__name__}")
            old_column_names = self.constraint_names.get_fields_to_columns(model, old_constraint.fields)
            old_name = self.constraint_names.constraint_name_for_model(
                model, UniqueConstraint(fields=tuple(old_column_names), name=old_constraint.name)
            )
            new_column_names = self.constraint_names.get_fields_to_columns(model, new_constraint.fields)
            new_c = UniqueConstraint(fields=tuple(new_column_names), name=new_constraint.name)
            new_name = self.constraint_names.constraint_name_for_model(model, new_c)
        if old_name == new_name:
            return
        if self.RENAME_CONSTRAINT_TEMPLATE:
            await self.run_sql(
                self.RENAME_CONSTRAINT_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    old_name=self.quote(old_name),
                    new_name=self.quote(new_name),
                )
            )
            return
        await self.remove_constraint(model, old_constraint)
        await self.add_constraint(model, new_constraint)

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
