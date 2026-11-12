from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.schema_references import SchemaReferences
from hare.dialects.base.client.schema_cache_invalidation import SchemaCacheInvalidation
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.plans.column_conversion_precondition import ColumnConversionPrecondition
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.client.sqlite_pragma_scope import SqlitePragmaScope
from hare.dialects.sqlite.constants import (
    SPATIALITE_METADATA_TABLE_NAMES,
    SQLITE_DEFAULT_FOREIGN_KEYS,
)
from hare.dialects.sqlite.indexes.separate_table_index import SeparateTableIndex
from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.dialects.sqlite.schema.columns.sqlite_column_backfill import SqliteColumnBackfill
from hare.dialects.sqlite.schema.columns.sqlite_column_conversion_check import SqliteColumnConversionCheck
from hare.dialects.sqlite.schema.columns.sqlite_column_narrowing_check import SqliteColumnNarrowingCheck
from hare.dialects.sqlite.schema.columns.sqlite_column_type_changes import SqliteColumnTypeChanges
from hare.dialects.sqlite.schema.columns.sqlite_kept_values_check import SqliteKeptValuesCheck
from hare.dialects.sqlite.schema.constants import (
    SQLITE_ADDED_COLUMN_DEFAULT_PATTERN,
    SQLITE_DELETE_ROW_SQL,
    SQLITE_FOREIGN_KEY_CHECK_SQL,
    SQLITE_GENERATED_PK_TEMPLATE,
    SQLITE_PRAGMA_ENABLED_VALUES,
)
from hare.dialects.sqlite.schema.constraints.sqlite_constraint_statements import SqliteConstraintStatements
from hare.dialects.sqlite.schema.indexes.sqlite_generated_index_names import SqliteGeneratedIndexNames
from hare.dialects.sqlite.schema.indexes.sqlite_index_statements import SqliteIndexStatements
from hare.dialects.sqlite.schema.indexes.sqlite_spatial_index_creation import SqliteSpatialIndexCreation
from hare.dialects.sqlite.schema.relations.sqlite_foreign_key_rebuild import SqliteForeignKeyRebuild
from hare.dialects.sqlite.schema.runtime_statements.sqlite_table_clearing import SqliteTableClearing
from hare.dialects.sqlite.schema.runtime_statements.sqlite_tenant_conditions import SqliteTenantConditions
from hare.dialects.sqlite.schema.tables.sqlite_table_comments import SqliteTableComments
from hare.dialects.sqlite.schema.tables.sqlite_table_rebuild import SqliteTableRebuild
from hare.dialects.sqlite.schema.triggers.sqlite_trigger_statements import SqliteTriggerStatements
from hare.exceptions import IntegrityError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.fields.field import Field


class SqliteSchemaEditor(BaseSchemaEditor):
    """The schema editor of SQLite - the DDL of its tables, which a change of most columns and constraints
    rebuilds, of its indexes and of its triggers."""

    column_type_changes_class = SqliteColumnTypeChanges
    column_type_changes: SqliteColumnTypeChanges
    column_backfill_class = SqliteColumnBackfill
    column_conversion_check_class = SqliteColumnConversionCheck
    column_conversion_check: SqliteColumnConversionCheck
    column_narrowing_check_class = SqliteColumnNarrowingCheck
    table_rebuild_class = SqliteTableRebuild
    table_comments_class = SqliteTableComments
    foreign_key_rebuild_class = SqliteForeignKeyRebuild
    index_statements_class = SqliteIndexStatements
    generated_index_names_class = SqliteGeneratedIndexNames
    constraint_statements_class = SqliteConstraintStatements
    trigger_statements_class = SqliteTriggerStatements
    table_clearing_class = SqliteTableClearing
    tenant_conditions_class = SqliteTenantConditions

    # The table's comment inside its parentheses - SQLite keeps the statement's text up to the
    # closing one only.
    TABLE_CREATE_TEMPLATE = "CREATE {prefix}TABLE {exists}{table_name} ({comment}{fields}){extra};"
    MANY_TO_MANY_TABLE_TEMPLATE = "CREATE TABLE {exists}{table_name} ({comment}{fields}){extra};"
    DELETE_TABLE_TEMPLATE = "DROP TABLE {table}"
    DELETE_FIELD_TEMPLATE = "ALTER TABLE {table} DROP COLUMN {column}"
    GENERATED_PK_TEMPLATE = SQLITE_GENERATED_PK_TEMPLATE
    #: SQLite has no ALTER TABLE ... RENAME CONSTRAINT at all - see rename_constraint() below.
    RENAME_CONSTRAINT_TEMPLATE = None
    #: A SQLite trigger has no function and no FOR EACH clause - the body runs once per row.
    #: {trigger_name} is passed already quoted.
    TRIGGER_CREATE_TEMPLATE = (
        "CREATE TRIGGER {trigger_name} {timing} {on} ON {table}\n{when_clause}BEGIN\n{body}\nEND;"
    )
    TRIGGER_CREATE_IF_NOT_EXISTS_TEMPLATE = TRIGGER_CREATE_TEMPLATE.replace(
        "CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1
    )
    TRIGGER_DROP_TEMPLATE = "DROP TRIGGER {trigger_name}"
    #: True while constraint_checking_disabled() has switched off foreign key enforcement that
    #: the connection normally has on - check_constraints() only verifies in that window.
    foreign_key_check_required = False

    async def run_sql(self, sql: str, *, recovery: StatementRecovery | None = None) -> None:
        """Runs DDL. In atomic mode each statement goes through ``execute()`` - ``executescript()``
        issues an implicit COMMIT. In ``collect_sql`` mode the SQL is only collected.
        """
        if self.collect_sql:
            await super().run_sql(sql, recovery=recovery)
            return
        self.run_statement_count += 1
        async with SchemaCacheInvalidation.run(self.client):
            if self.atomic_migration or self.client.is_transaction_client:
                for statement in SqliteClient.split_script(sql):
                    await self.client.execute(statement, recovery=recovery)
            else:
                await self.client.execute_script(sql, recovery=recovery)

    def get_index_name_sql(self, index_name: str, schema: str | None) -> str:
        # SQLite names the attached database of a new index or trigger with the object's name.
        return self.qualify_table_name(index_name, schema)

    def get_indexed_table_sql(self, table_name: str, schema: str | None) -> str:
        # The table of an index or trigger is one of the object's own attached database.
        return self.quote(table_name)

    def get_referenced_table_sql(self, table_name: str, schema: str | None) -> str:
        # A foreign key references a table of its own attached database.
        return self.quote(table_name)

    @asynccontextmanager
    async def constraint_checking_disabled(self) -> AsyncGenerator[None]:
        """Switches ``PRAGMA foreign_keys`` off for the block and restores what was configured. A table
        rebuild drops the table, and with foreign keys on, that deletes its rows first and cascades
        into the tables referencing it. The pragma can only be changed outside a transaction - the
        executor enters this block before opening one.
        """
        original = self.client.pragmas.get("foreign_keys", SQLITE_DEFAULT_FOREIGN_KEYS)  # type: ignore[attr-defined]
        self.foreign_key_check_required = str(original).strip().lower() in SQLITE_PRAGMA_ENABLED_VALUES
        switched_off = SqliteClient.foreign_keys_switched_off
        token = switched_off.set(switched_off.get() | {self.client.connection_alias})
        try:
            async with SqlitePragmaScope.run(self.client, "foreign_keys", "OFF", previous=original):
                yield
        finally:
            switched_off.reset(token)
            self.foreign_key_check_required = False

    async def check_constraints(self) -> None:
        """Raises if the schema changes made while foreign key enforcement was off left
        dangling references behind.

        Raises:
            IntegrityError: ``PRAGMA foreign_key_check`` reported at least one violation.
        """
        if self.collect_sql or not self.foreign_key_check_required:
            return
        _, rows = await self.client.execute(SQLITE_FOREIGN_KEY_CHECK_SQL)
        rows = await self.delete_spatial_metadata_orphans(rows)
        if not rows:
            return
        violations = "; ".join(
            f"{row['table']} rowid={row['rowid']} references missing row in {row['parent']}" for row in rows
        )
        raise IntegrityError(f"Migration left {len(rows)} foreign key violation(s): {violations}")

    async def delete_spatial_metadata_orphans(self, violations: Sequence[Any]) -> list[Any]:
        """Deletes the rows of SpatiaLite's spatial metadata left naming a column the migration
        unregistered - the ``ON DELETE CASCADE`` of those tables, which doesn't run while foreign keys
        are off.

        Args:
            violations: The rows of ``PRAGMA foreign_key_check``.

        Returns:
            The other violations.
        """
        remaining = []
        for violation in violations:
            if violation["table"] in SPATIALITE_METADATA_TABLE_NAMES and violation["parent"] in (
                SPATIALITE_METADATA_TABLE_NAMES
            ):
                await self.client.execute(
                    SQLITE_DELETE_ROW_SQL.format(table=self.quote(violation["table"])),
                    [violation["rowid"]],
                )
            else:
                remaining.append(violation)
        return remaining

    def get_add_field_plan(
        self, old_model: type[Model], new_model: type[Model], field_name: str, *, foreign_key_not_valid: bool = False
    ) -> SchemaChangePlan:
        """Choose a prepared rebuild for additions unsupported by ALTER TABLE.

        SQLite does not check old rows when adding a foreign key, so requesting an
        unvalidated key does not change its SQL. Other additions use the shared
        backfill and index plan; required Python defaults finish with a rebuild.
        """
        field = new_model._meta.fields_map[field_name]
        if not isinstance(field, ManyToManyFieldInstance) and (
            (isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1)
            or (field.generated and not field.pk)
            or (
                field.has_db_default()
                and not SQLITE_ADDED_COLUMN_DEFAULT_PATTERN.fullmatch(
                    self.column_definitions.get_field_db_default_sql(field, new_model)
                )
            )
        ):
            return self.table_rebuild.get_rebuild_plan(old_model, new_model)
        return super().get_add_field_plan(old_model, new_model, field_name, foreign_key_not_valid=False)

    def get_add_constraint_plan(self, model: type[Model], constraint: Any) -> SchemaChangePlan:
        """Adds a constraint: a CHECK by rebuilding the table from the model, which declares it; a unique
        constraint as a unique index under its name.

        Args:
            model: The model with the constraint.
            constraint: The constraint.
        """
        if isinstance(constraint, CheckConstraint):
            return self.table_rebuild.get_rebuild_plan(model, model)
        return super().get_add_constraint_plan(model, constraint)

    def get_remove_constraint_plan(self, model: type[Model], constraint: Any) -> SchemaChangePlan:
        """A CHECK requires rebuilding; uniqueness is removed through its index."""
        if isinstance(constraint, CheckConstraint):
            return self.table_rebuild.get_rebuild_plan(model, model)
        name = self.constraint_names.constraint_name_for_model(model, constraint)
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.DROP_INDEX_TEMPLATE.format(
                        name=self.qualify_table_name(name, model._meta.schema),
                    )
                ),
            )
        )

    def get_rename_constraint_plan(
        self, model: type[Model], old_constraint: Any, new_constraint: Any
    ) -> SchemaChangePlan:
        """A CHECK is recreated with its table; a unique index uses the shared rename plan."""
        if isinstance(old_constraint, CheckConstraint):
            return self.table_rebuild.get_rebuild_plan(model, model)
        return super().get_rename_constraint_plan(model, old_constraint, new_constraint)

    @staticmethod
    def get_separate_table_indexes(model: type[Model]) -> list[SeparateTableIndex]:
        """The model's indexes kept in tables of their own - full-text and spatial ones - which
        outlive a DROP TABLE of the model's table and read it and its columns by name.

        Args:
            model: The model.

        Returns:
            The indexes.
        """
        return [index for index in model._meta.indexes if isinstance(index, SeparateTableIndex)]

    def get_separate_index_drop_plan(
        self, model: type[Model], indexes: Iterable[SeparateTableIndex], table_name: str | None = None
    ) -> SchemaChangePlan:
        """Prepare all statements removing separate index tables for the current physical name."""
        return SchemaChangePlan(
            tuple(
                SchemaStatement(
                    "\n".join(
                        index.get_drop_sqls(
                            self,
                            table_name or model._meta.db_table,
                            model._meta.get_column_names(index.fields),
                            model._meta.schema,
                        )
                    )
                )
                for index in indexes
            )
        )

    def get_add_index_plan(self, model: type[Model], index: Index, concurrently: bool = False) -> SchemaChangePlan:
        """Keep SpatiaLite's result checks in both standalone and composed plans."""
        if not isinstance(index, SpatialiteIndex):
            return super().get_add_index_plan(model, index)
        return SchemaChangePlan(
            (
                SqliteSpatialIndexCreation(
                    tuple(index.get_create_sqls(self, model, safe=False)),
                    f"{index!r} of {model.__name__}",
                ),
            )
        )

    def get_table_rename_plan(self, model: type[Model], old_name: str, new_name: str) -> SchemaChangePlan:
        """Prepare separate-table index removal, the rename and index recreation together."""
        if old_name == new_name:
            return SchemaChangePlan(())
        indexes = self.get_separate_table_indexes(model)
        return SchemaChangePlan(
            (
                *self.get_separate_index_drop_plan(model, indexes, old_name).steps,
                *super().get_table_rename_plan(model, old_name, new_name).steps,
                *(step for index in indexes for step in self.get_add_index_plan(model, index).steps),
            )
        )

    def get_delete_model_plan(self, model: type[Model]) -> SchemaChangePlan:
        """Remove separate index tables before the table and its automatic through tables."""
        plan = super().get_delete_model_plan(model)
        drops = self.get_separate_index_drop_plan(model, self.get_separate_table_indexes(model))
        return SchemaChangePlan((*drops.steps, *plan.steps), before=plan.before)

    def get_remove_field_plan(
        self, old_model: type[Model], new_model: type[Model], field: Field[Any]
    ) -> SchemaChangePlan:
        """Prepare an in-place removal when supported, otherwise a complete rebuild."""
        if isinstance(field, ManyToManyFieldInstance) or self._drops_column_in_place(old_model, field):
            return super().get_remove_field_plan(old_model, new_model, field)
        return self.table_rebuild.get_rebuild_plan(old_model, new_model)

    def _drops_column_in_place(self, model: type[Model], field: Field[Any]) -> bool:
        """Whether ``ALTER TABLE ... DROP COLUMN`` drops the field's column instead of a table rebuild -
        decided by the model alone. SQLite refuses a column that is a key, unique, indexed, a foreign key
        or named by a CHECK, a generated column, a trigger or a view - such a column, and any column of a
        model with a CHECK constraint, a generated column or a trigger, goes through the rebuild. An object
        the migrations don't know of keeping the column makes the database refuse it.

        Args:
            model: The model the field is removed from.
            field: The removed field.

        Returns:
            True when the column is dropped in place.
        """
        meta = model._meta
        if (
            isinstance(field, ForeignKeyFieldInstance)
            or field.pk
            or field.unique
            or field.index
            or field.generated
            or field.reference is not None
            or field.model_field_name not in meta.fields_db_projection
            or meta.triggers
            # The triggers of an index kept in a table of its own name the table's columns.
            or self.get_separate_table_indexes(model)
            # A generated column - an auto-incremented primary key is "generated" too, and harmless.
            or any(other_field.generated and not other_field.pk for other_field in meta.fields_map.values())
            or any(isinstance(constraint, CheckConstraint) for constraint in meta.constraints)
        ):
            return False
        for index in meta.indexes:
            if not isinstance(index, Index):
                index = Index(fields=tuple(index))
            if (
                index.declared_expressions
                or isinstance(index, PartialIndex)
                or field.model_field_name in index.include
                or SchemaReferences.references_field(SchemaReferences.get_index_field_names(index), field)
            ):
                return False
        for constraint in getattr(meta, ModelOption.CONSTRAINTS, None) or ():
            if not isinstance(constraint, UniqueConstraint) or (
                constraint.condition is not None
                or field.model_field_name in constraint.include
                or SchemaReferences.references_field(SchemaReferences.get_constraint_field_names(constraint), field)
            ):
                return False
        return True

    def get_alter_field_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_name: str,
        candidate_referencing_models: Sequence[type[Model]] = (),
    ) -> SchemaChangePlan:
        """Keep unchanged columns in place while preparing their validation and Python rewrite."""
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        if not self.column_type_changes.keeps_table(old_field, new_field, self.client.dialect):
            return super().get_alter_field_plan(old_model, new_model, field_name, candidate_referencing_models)
        rewrite = self.column_backfill.get_stored_value_rewrite_plan(new_model, old_field, new_field)
        steps: list[SchemaStep] = [
            SqliteKeptValuesCheck(
                self.qualify_table_name(new_model._meta.db_table, new_model._meta.schema),
                self.quote(self.table_rebuild.get_remake_column_name(new_field)),
                copy(new_field),
            )
        ]
        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if old_indexed != new_indexed:
            columns = (
                new_field.source_fields
                if isinstance(new_field, ForeignKeyFieldInstance)
                else (self.table_rebuild.get_remake_column_name(new_field),)
            )
            index = Index(fields=tuple(columns))
            if not self.index_statements.is_declared_by_another_entry(
                new_model, index, model_declares_index=new_indexed
            ):
                if new_indexed:
                    steps.extend(self.get_add_index_plan(new_model, index).steps)
                else:
                    steps.append(SchemaStatement(self.index_statements.get_index_drop_sql(new_model, index)))
        return SchemaChangePlan(
            (*steps, *rewrite.steps),
            before=self.table_creation.get_definition(old_model),
            after=self.table_creation.get_definition(new_model),
        )

    def get_alter_column_plan(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any]
    ) -> SchemaChangePlan:
        """Prepare a rename or rebuild, checking stored values before destructive DDL.

        A column rename must precede the rebuild because the database updates foreign
        references during RENAME COLUMN. Checks still use the original column name;
        the copy prepared after the rename uses the renamed source.
        """
        self.raise_if_generated_field_changes(old_field, new_field)
        old_column = self.table_rebuild.get_remake_column_name(old_field)
        new_column = self.table_rebuild.get_remake_column_name(new_field)
        table = self.qualify_table_name(model._meta.db_table, model._meta.schema)
        steps: list[SchemaStep] = []
        only_rename = self.column_type_changes.only_renames_column(old_field, new_field, self.client.dialect)
        if not only_rename:
            steps.append(
                ColumnConversionPrecondition(table, self.quote(old_column), copy(old_field), copy(new_field), True)
            )
        if old_column != new_column and not isinstance(old_field, ForeignKeyFieldInstance):
            indexes = [
                index for index in self.get_separate_table_indexes(model) if new_field.model_field_name in index.fields
            ]
            for index in indexes:
                columns = [
                    old_column if column == new_column else column
                    for column in model._meta.get_column_names(index.fields)
                ]
                steps.append(
                    SchemaStatement(
                        "\n".join(index.get_drop_sqls(self, model._meta.db_table, columns, model._meta.schema))
                    )
                )
            steps.append(
                SchemaStatement(
                    self.RENAME_FIELD_TEMPLATE.format(
                        table=table,
                        old_column=self.quote(old_column),
                        new_column=self.quote(new_column),
                    )
                )
            )
            if only_rename:
                steps.extend(step for index in indexes for step in self.get_add_index_plan(model, index).steps)
                return SchemaChangePlan(tuple(steps))
            old_field = copy(old_field)
            old_field.source_field = new_column
        plan = self.table_rebuild.get_rebuild_plan(model, model, altered_fields=[(old_field, new_field)])
        return SchemaChangePlan((*steps, *plan.steps), after=plan.after)
