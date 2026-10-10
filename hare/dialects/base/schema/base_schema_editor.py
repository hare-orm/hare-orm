from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
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
from hare.dialects.base.schema.columns.column_definitions import ColumnDefinitions
from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
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
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral


class BaseSchemaEditor:
    """Writes the DDL of the models and of each migration operation: tables, fields, indexes,
    constraints, triggers, schemas, extensions and table rebuilds. A dialect subclasses it."""

    column_definitions_class: ClassVar[type[ColumnDefinitions]] = ColumnDefinitions
    column_type_changes_class: ClassVar[type[ColumnTypeChanges]] = ColumnTypeChanges
    column_backfill_class: ClassVar[type[ColumnBackfill]] = ColumnBackfill
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
    FIELD_TEMPLATE = "{name} {type}{nullable}{unique}{primary}{default}{comment}"
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
    # DROP INDEX/ALTER INDEX name no table: {name}/{old_name} are passed schema-qualified,
    # {new_name} as a plain quoted name. DROP INDEX is also given the index's {table} - for a
    # database whose index belongs to its table.
    DROP_INDEX_TEMPLATE = "DROP INDEX {name}"
    #: How an index is renamed in place; None drops it and creates it again under the new name.
    RENAME_INDEX_TEMPLATE: str | None = None
    #: The same, doing nothing when the index doesn't exist; None renames it with rename_index().
    RENAME_INDEX_IF_EXISTS_TEMPLATE: str | None = None
    RENAME_CONSTRAINT_TEMPLATE: str | None = "ALTER TABLE {table} RENAME CONSTRAINT {old_name} TO {new_name}"

    def __init__(self, connection: DatabaseClient, atomic: bool = True, collect_sql: bool = False) -> None:
        self.client = connection
        self.atomic = atomic
        self.atomic_migration = connection.features.can_rollback_ddl and atomic
        self.collect_sql = collect_sql
        self.collected_sql: list[str] = []
        self.column_definitions = self.column_definitions_class(self)
        self.column_type_changes = self.column_type_changes_class(self)
        self.column_backfill = self.column_backfill_class(self)
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
        self.table_locks = self.table_locks_class(self)
        self.table_clearing = self.table_clearing_class(self)
        self.tenant_conditions = self.tenant_conditions_class(self)

    async def run_sql(self, sql: str) -> None:
        """Execute DDL SQL. Subclasses may override for backend-specific handling.

        If ``collect_sql`` is True, append the SQL to ``collected_sql`` instead
        of executing it.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        ProtectConstraintDeferral.forget_constraint_names()
        await self.client.execute_script(sql)

    @asynccontextmanager
    async def constraint_checking_disabled(self) -> AsyncGenerator[None]:
        """No-op by default - only SQLite's own table-rebuild approach (SqliteSchemaEditor's
        override) needs this. Postgres supports a real ALTER TABLE for everything this editor
        does, so it never has to drop and recreate a table just to change one column."""
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
        schema = model._meta.schema
        await self.materialized_views.drop_model_materialized_views(model)
        await self.dictionaries.drop_model_dictionaries(model)
        await self.views.drop_model_views(model)
        for field_name in sorted(model._meta.many_to_many_fields):
            field = cast("ManyToManyFieldInstance[Model]", model._meta.fields_map[field_name])
            if field.through_model is not None:
                # A ManyToManyField(through=SomeModel)'s table belongs to SomeModel, which gets
                # its own separate DropModel operation when it's actually meant to go away -
                # deleting THIS model must not also drop a table another model still owns.
                continue
            await self.run_sql(self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(field.through, schema)))

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
            db_field = model._meta.fields_db_projection[key_field_name]
            key_field = model._meta.fields_map[key_field_name]
            field_definition = self.column_definitions.get_field_sql(
                db_field=db_field,
                field_type=self.column_definitions.get_table_column_type(model, key_field),
                nullable=key_field.null,
                unique=False,
                is_pk=False,
                comment="",
            )
            await self.run_sql(
                self.ADD_FIELD_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    definition=field_definition,
                )
            )
        if not self.foreign_key_rebuild.creates_foreign_key(field):
            return
        constraint = self.table_creation.get_composite_foreign_key_constraint(model, field)
        if foreign_key_not_valid:
            await self.foreign_key_rebuild.add_foreign_key_constraint_not_valid(model, constraint)
        else:
            await self.add_constraint(model, constraint)

    async def add_field(self, model: type[Model], field_name: str, *, foreign_key_not_valid: bool = False) -> None:
        """Adds a field's column (or a many-to-many relation's through table) to an existing table.

        Args:
            model: The model rendered from the state with the field.
            field_name: The field's name.
            foreign_key_not_valid: For a relation with a database constraint: add the FOREIGN KEY
                without checking the existing rows (``add_foreign_key_constraint_not_valid()``) -
                a later ``validate_constraint()`` checks them.
        """
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
                field_definition = self.column_definitions.get_foreign_key_field_definition(
                    model, key_field_name, with_reference=False
                )
                unvalidated_foreign_key = self.table_creation.get_single_column_foreign_key_constraint(
                    model, key_field_name
                )
            else:
                field_definition = self.column_definitions.get_foreign_key_field_definition(model, key_field_name)
        else:
            db_field = model._meta.fields_db_projection[field_name]
            # Added nullable first, filled, then made NOT NULL - a NOT NULL column with only a
            # Python default can't be added to a table that already holds rows in one step.
            needs_backfill = self.column_backfill.needs_added_column_backfill(field)
            comment = self.column_definitions.get_column_trailing_sql(model, field_name, field.description)
            # A column the database computes is declared as the table's own one is.
            generated_field_definition = self.column_definitions.get_non_pk_generated_field_sql(
                model, field, db_field, comment
            )
            field_definition = generated_field_definition or self.column_definitions.get_field_sql(
                db_field=db_field,
                field_type=self.column_definitions.get_table_column_type(model, field),
                nullable=field.null or needs_backfill,
                unique=field.unique,
                is_pk=field.pk,
                comment=comment,
            )

        if field.has_db_default():
            if hasattr(field.db_default, "get_sql"):
                field_definition += f" DEFAULT {self.column_definitions.get_db_default_sql(field.db_default)}"
            else:
                db_value = self.client.dialect.types.get_db_value(field, field.db_default, model)
                escaped = self.client.dialect.literals.get_literal_sql(db_value)
                field_definition += f" DEFAULT {escaped}"

        await self.run_sql(
            self.ADD_FIELD_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                definition=field_definition,
            )
        )
        if needs_backfill:
            await self.column_backfill.backfill_added_column(model, field, db_field)
        if unvalidated_foreign_key is not None:
            await self.foreign_key_rebuild.add_foreign_key_constraint_not_valid(model, unvalidated_foreign_key)

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
            held_indexes = self.index_statements.get_indexes_covering_field(model, new_field, old_indexed)
            for index in held_indexes:
                await self.remove_index(model, index)
        if old_sql_type != new_sql_type:
            dependent_generated_fields = await self._alter_column_type(
                model, qualified_table, old_field, new_field, new_sql_type
            )
        if old_field.null != new_field.null:
            actions.append(await self._get_nullability_change_sql(model, qualified_table, new_field, new_sql_type))
        await self._alter_column_index_and_uniqueness(model, old_field, new_field, holds_indexes=bool(held_indexes))
        if old_field.description != new_field.description:
            await self.table_comments.alter_column_comment(model, old_field, new_field)
        default_change_sql = self._get_default_change_sql(model, qualified_table, old_field, new_field)
        if default_change_sql is not None:
            actions.append(default_change_sql)

        if actions:
            result_query = ";\n".join(actions)
            await self.run_sql(result_query)

        for position, index in enumerate(held_indexes):
            # The column's own index - held first - comes back only while the field still has one.
            if position or not old_indexed or new_indexed:
                await self.add_index(model, index)

        for generated_field in dependent_generated_fields:
            await self.add_field(model, generated_field.model_field_name)

    async def _alter_column_index_and_uniqueness(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any], *, holds_indexes: bool
    ) -> None:
        """Adds or drops a column's own index and its unique constraint.

        Args:
            model: The model.
            old_field: The field before the change.
            new_field: The field after it.
            holds_indexes: Whether the column's indexes were dropped for the change - its own index
                then isn't there to drop.
        """
        new_db_field = new_field.source_field or new_field.model_field_name
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if old_indexed != new_indexed:
            index = Index(fields=(new_db_field,))
            if new_indexed:
                await self.add_index(model, index)
            elif not holds_indexes:
                await self.remove_index(model, index)
        if old_field.unique != new_field.unique:
            constraint = UniqueConstraint(fields=(new_db_field,))
            if new_field.unique:
                await self.add_constraint(model, constraint)
            else:
                await self.remove_constraint(model, constraint)

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
        if self.client.features.truncates_values_on_type_change and not self.collect_sql:
            # A `::sql_type` cast truncates a too long string and rounds a too precise decimal
            # without an error - checked against the table's data before the ALTER runs.
            await self.column_narrowing_check.check_alter_field_narrowing_data_loss(
                qualified_table, old_field, new_field, self.quote(new_db_field)
            )
        # Postgres doesn't change the type of a column a generated column depends on - the
        # dependent generated columns are dropped and created again afterwards.
        dependent_generated_fields = self.column_type_changes.get_generated_fields_depending_on_column(
            model, new_db_field
        )
        for generated_field in dependent_generated_fields:
            await self.remove_field(model, generated_field)
        changes = self.ALTER_FIELD_TYPE_TEMPLATE.format(
            column=self.quote(new_db_field),
            sql_type=self.column_definitions.get_altered_column_type(new_sql_type, new_field.null),
        )
        await self.run_sql(self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes))
        return dependent_generated_fields

    async def _get_nullability_change_sql(
        self, model: type[Model], qualified_table: str, new_field: Field[Any], new_sql_type: str
    ) -> str:
        """The ALTER making a column nullable or not. A column made NOT NULL gets the field's
        default in its NULL rows first - written at once.

        Args:
            model: The model.
            qualified_table: The model's quoted table.
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
            changes = self.ALTER_FIELD_NOT_NULL_TEMPLATE.format(
                column=self.quote(new_db_field),
                sql_type=self.column_definitions.get_altered_column_type(new_sql_type, False),
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
            if hasattr(new_field.db_default, "get_sql"):
                default_sql = self.column_definitions.get_db_default_sql(new_field.db_default)
            else:
                db_value = self.client.dialect.types.get_db_value(new_field, new_field.db_default, model)
                default_sql = self.client.dialect.literals.get_literal_sql(db_value)
            changes = self.ALTER_FIELD_SET_DEFAULT_TEMPLATE.format(column=column, default=default_sql)
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
            # A relation field and a plain column can't be converted into each other by an ALTER.
            raise ConfigurationError(
                f"Converting field '{field_name}' between a relation field and a plain column "
                "is not supported by AlterField - remove the field and add it back instead."
            )

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
                for old_key_field, new_key_field in key_field_pairs:
                    await self.alter_column(new_model, old_key_field, new_key_field)
                await self.foreign_key_rebuild.restore_relation_foreign_keys(new_model, new_field)
                await self.foreign_key_rebuild.alter_composite_relation_index(new_model, old_field, new_field)
                return
            if self.foreign_key_rebuild.foreign_key_changed(old_field, new_field):
                await self.foreign_key_rebuild.alter_foreign_key_on_delete(new_model, old_source, old_field, new_field)
            for old_key_field, new_key_field in key_field_pairs:
                await self.alter_column(new_model, old_key_field, new_key_field)
            await self.foreign_key_rebuild.alter_composite_relation_index(new_model, old_field, new_field)
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

    async def remove_field(self, model: type[Model], field: Field[Any]) -> None:
        if isinstance(field, ManyToManyFieldInstance):
            if field.through_model is not None:
                # SomeModel (through=SomeModel) keeps its own table - dropped via its own
                # DropModel operation, not by removing this M2M field.
                return
            await self.run_sql(
                self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(field.through, model._meta.schema))
            )
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

        if isinstance(field, ForeignKeyFieldInstance):
            source_field = field.source_field or field.model_field_name
            field = model._meta.fields_map[source_field]
            # No explicit constraint drop needed here - see the composite-target branch above
            # for why (verified against Postgres: this column's own FK/CHECK/UNIQUE/index all
            # get dropped automatically along with it).
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
        if not self.client.features.supports_unique_constraints:
            return
        self.constraint_statements.check_unique_constraint_supported(constraint)
        constraint_column_names = self.constraint_names.get_fields_to_columns(model, constraint.fields)
        if constraint.condition:
            # A partial unique constraint is a unique index with a WHERE clause - an index takes a
            # condition, a table constraint doesn't.
            index_name = self.constraint_names.get_partial_unique_index_name(model, constraint)
            index_sql = (
                f"CREATE UNIQUE INDEX {self.quote(index_name)} "
                f"ON {self.qualify_table_name(model._meta.db_table, model._meta.schema)} "
                f"({', '.join([self.quote(field_name) for field_name in constraint_column_names])})"
                f"{constraint.get_include_sql(model, self.client.dialect, self.quote)}"
                f"{constraint.get_nulls_sql(self.client.dialect)} "
                f"WHERE {ConstraintCondition.get_sql(constraint.condition, model, self.client)}"
            )
            await self.run_sql(index_sql + ";")
            return
        constraint_name = self.constraint_names.constraint_name_for_model(
            model, UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        )
        constraint_sql = self.UNIQUE_CONSTRAINT_CREATE_TEMPLATE.format(
            index_name=self.quote(constraint_name),
            nulls=constraint.get_nulls_sql(self.client.dialect),
            fields=self.column_definitions.get_key_columns_sql(constraint_column_names, constraint.without_overlaps),
            include=constraint.get_include_sql(model, self.client.dialect, self.quote),
        )
        if isinstance(constraint, UniqueConstraint) and constraint.deferrable:
            constraint_sql += " DEFERRABLE INITIALLY " + ("DEFERRED" if constraint.initially_deferred else "IMMEDIATE")
        await self.run_sql(
            self.ADD_CONSTRAINT_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=constraint_sql,
            )
        )

    async def remove_constraint(
        self,
        model: type[Model],
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        if isinstance(constraint, (CheckConstraint, ExclusionConstraint, ForeignKeyConstraint)):
            await self.run_sql(
                self.DELETE_CONSTRAINT_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    name=self.quote(constraint.name),
                )
            )
            return
        if not self.client.features.supports_unique_constraints:
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
        constraint_name = await self.constraint_names.get_constraint_name(model, constraint)
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
        if isinstance(old_constraint, UniqueConstraint) and not self.client.features.supports_unique_constraints:
            return
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
            # The old name is looked up in the database, as remove_constraint() does; the new one
            # doesn't exist yet.
            old_name = await self.constraint_names.get_constraint_name(model, old_constraint)
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
