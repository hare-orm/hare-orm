from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.constants import (
    SPATIALITE_INDEX_RESULT_COLUMN,
    SPATIALITE_METADATA_TABLE_NAMES,
    SQLITE_DEFAULT_FOREIGN_KEYS,
)
from hare.dialects.sqlite.indexes.separate_table_index import SeparateTableIndex
from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.dialects.sqlite.schema.columns.sqlite_column_backfill import SqliteColumnBackfill
from hare.dialects.sqlite.schema.columns.sqlite_column_conversion_check import SqliteColumnConversionCheck
from hare.dialects.sqlite.schema.columns.sqlite_column_narrowing_check import SqliteColumnNarrowingCheck
from hare.dialects.sqlite.schema.columns.sqlite_column_type_changes import SqliteColumnTypeChanges
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
from hare.dialects.sqlite.schema.relations.sqlite_foreign_key_rebuild import SqliteForeignKeyRebuild
from hare.dialects.sqlite.schema.runtime_statements.sqlite_table_clearing import SqliteTableClearing
from hare.dialects.sqlite.schema.runtime_statements.sqlite_tenant_conditions import SqliteTenantConditions
from hare.dialects.sqlite.schema.tables.sqlite_table_comments import SqliteTableComments
from hare.dialects.sqlite.schema.tables.sqlite_table_rebuild import SqliteTableRebuild
from hare.dialects.sqlite.schema.triggers.sqlite_trigger_statements import SqliteTriggerStatements
from hare.exceptions import IntegrityError, OperationalError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:
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

    async def run_sql(self, sql: str) -> None:
        """Runs DDL. In atomic mode each statement goes through ``execute()`` - ``executescript()``
        issues an implicit COMMIT. In ``collect_sql`` mode the SQL is only collected.
        """
        if self.collect_sql:
            await super().run_sql(sql)
            return
        self.run_statement_count += 1
        if self.atomic_migration or self.client.is_transaction_client:
            for statement in SqliteClient.split_script(sql):
                await self.client.execute(statement)
        else:
            await self.client.execute_script(sql)

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
        original = cast("SqliteClient", self.client).pragmas.get("foreign_keys", SQLITE_DEFAULT_FOREIGN_KEYS)
        await self.client.execute_script("PRAGMA foreign_keys = OFF")
        self.foreign_key_check_required = str(original).strip().lower() in SQLITE_PRAGMA_ENABLED_VALUES
        switched_off = SqliteClient.foreign_keys_switched_off
        token = switched_off.set(switched_off.get() | {self.client.connection_alias})
        try:
            yield
        finally:
            switched_off.reset(token)
            self.foreign_key_check_required = False
            await self.client.execute_script(f"PRAGMA foreign_keys = {original}")

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

    async def add_field(
        self, old_model: type[Model], new_model: type[Model], field_name: str, *, foreign_key_not_valid: bool = False
    ) -> None:
        # SQLite never checks the existing rows against a FOREIGN KEY it adds - foreign_key_not_valid
        # changes nothing.
        model = new_model
        field = model._meta.fields_map[field_name]
        if isinstance(field, ManyToManyFieldInstance):
            table_string = self.table_creation.get_many_to_many_table_definition(model, field)
            if table_string:
                await self.run_sql(table_string)
            return
        if (
            (isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1)
            or (field.generated and not field.pk)
            or (
                field.has_db_default()
                and not SQLITE_ADDED_COLUMN_DEFAULT_PATTERN.fullmatch(
                    self.column_definitions.get_field_db_default_sql(field, model)
                )
            )
        ):
            # SQLite adds neither a table-level FOREIGN KEY, a STORED generated column nor a column with
            # a DEFAULT other than a constant (an expression, CURRENT_TIMESTAMP) to a table holding rows -
            # the table is rebuilt.
            await self.table_rebuild.remake_table(old_model, new_model)
            return
        needs_backfill = False
        if isinstance(field, ForeignKeyFieldInstance):
            column_field_name = field.source_field or field_name
            field_definition = self.column_definitions.get_column_definition(model, column_field_name)
        else:
            column_field_name = field_name
            # SQLite refuses ADD COLUMN ... NOT NULL without a database default, so a column with
            # only a Python default is added nullable, filled, then rebuilt as NOT NULL.
            needs_backfill = self.column_backfill.needs_added_column_backfill(field)
            field_definition = self.column_definitions.get_column_definition(
                model, column_field_name, nullable=field.null or needs_backfill
            )
        db_field = model._meta.fields_db_projection[column_field_name]
        await self.run_sql(
            self.ADD_FIELD_TEMPLATE.format(
                table=self.qualify_table_name(model._meta.db_table, model._meta.schema), definition=field_definition
            )
        )
        rebuilt = False
        if needs_backfill:
            rebuilt = await self.column_backfill.backfill_added_column(model, field, db_field)
        if not rebuilt:
            # A rebuilt table has its uniqueness and indexes already.
            for constraint in self.table_creation.get_field_unique_constraints(model):
                if constraint.fields == (db_field,):
                    await self.add_constraint(model, constraint)
            await self.add_field_indexes(model, field)

    async def add_constraint(self, model: type[Model], constraint: Any) -> None:
        """Adds a constraint: a CHECK by rebuilding the table from the model, which declares it; a unique
        constraint as a unique index under its name.

        Args:
            model: The model with the constraint.
            constraint: The constraint.
        """
        if isinstance(constraint, CheckConstraint):
            await self.table_rebuild.remake_table(model, model)
            return
        await super().add_constraint(model, constraint)

    async def remove_constraint(self, model: type[Model], constraint: Any) -> None:
        """Drops a constraint: a CHECK by rebuilding the table from the model, which no longer declares it;
        a unique constraint by dropping its index.

        Args:
            model: The model without the constraint.
            constraint: The constraint.
        """
        if isinstance(constraint, CheckConstraint):
            await self.table_rebuild.remake_table(model, model)
            return
        constraint_column_names = self.constraint_names.get_fields_to_columns(model, constraint.fields)
        constraint_name = self.constraint_names.constraint_name_for_model(
            model, UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        )
        await self.run_sql(
            self.DROP_INDEX_TEMPLATE.format(name=self.qualify_table_name(constraint_name, model._meta.schema))
        )

    async def rename_constraint(self, model: type[Model], old_constraint: Any, new_constraint: Any) -> None:
        """Renames a constraint: a CHECK, which lives in ``CREATE TABLE``, by rebuilding the table from the
        model; a unique constraint by dropping its index and creating it under the new name.
        """
        if isinstance(old_constraint, CheckConstraint):
            await self.table_rebuild.remake_table(model, model)
            return
        await super().rename_constraint(model, old_constraint, new_constraint)

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

    async def drop_separate_table_indexes(
        self, model: type[Model], indexes: Iterable[SeparateTableIndex], table_name: str | None = None
    ) -> None:
        """Drops indexes of a model kept in tables of their own, each only when it exists.

        Args:
            model: The indexed model.
            indexes: The indexes.
            table_name: The indexed table as the database has it now - the model's by default.
        """
        for index in indexes:
            await self.run_sql(
                "\n".join(
                    index.get_drop_sqls(
                        self,
                        table_name or model._meta.db_table,
                        model._meta.get_column_names(index.fields),
                        model._meta.schema,
                    )
                )
            )

    async def add_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Creates an index - SpatiaLite answers each step of a spatial index with 1, or 0 when it
        refused it, which is raised."""
        if not isinstance(index, SpatialiteIndex) or self.collect_sql:
            await super().add_index(model, index, concurrently)
            return
        for step, sql in enumerate(index.get_create_sqls(self, model, safe=False)):
            rows = await self.client.execute_dicts(sql.removesuffix(";"))
            if rows and rows[0][SPATIALITE_INDEX_RESULT_COLUMN] == 1:
                continue
            reason = (
                "SpatiaLite refused to register the column - a row's geometry is of another type, SRID or "
                "dimensions than the field declares"
                if step == 0
                else "SpatiaLite refused to create its R*Tree"
            )
            raise OperationalError(f"{index!r} of {model.__name__} wasn't created: {reason}")

    async def rename_table(self, model: type[Model], old_name: str, new_name: str) -> None:
        """Renames a table - its indexes kept in tables of their own read it by name: dropped under
        the old name first and created again for the new one."""
        if old_name == new_name:
            return
        separate_table_indexes = self.get_separate_table_indexes(model)
        await self.drop_separate_table_indexes(model, separate_table_indexes, old_name)
        await super().rename_table(model, old_name, new_name)
        for index in separate_table_indexes:
            await self.add_index(model, index)

    async def delete_model(self, model: type[Model]) -> None:
        # A SQLite trigger has no function of its own, and DROP TABLE drops the table's triggers. An index
        # kept in a table of its own outlives the table - dropped first.
        await self.drop_separate_table_indexes(model, self.get_separate_table_indexes(model))
        await self.many_to_many_through_tables.drop_model_through_tables(model)
        await self.run_sql(
            self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(model._meta.db_table, model._meta.schema))
        )

    async def remove_field(self, old_model: type[Model], new_model: type[Model], field: Field[Any]) -> None:
        if isinstance(field, ManyToManyFieldInstance):
            await self.many_to_many_through_tables.drop_through_table(old_model, field)
            return
        if self._drops_column_in_place(old_model, field):
            await self.run_sql(
                self.DELETE_FIELD_TEMPLATE.format(
                    table=self.qualify_table_name(old_model._meta.db_table, old_model._meta.schema),
                    column=self.quote(old_model._meta.fields_db_projection[field.model_field_name]),
                )
            )
            return
        await self.table_rebuild.remake_table(old_model, new_model)

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
                index.expressions
                or isinstance(index, PartialIndex)
                or field.model_field_name in index.include
                or self.table_rebuild.index_references_field(index, field)
            ):
                return False
        for constraint in getattr(meta, ModelOption.CONSTRAINTS, None) or ():
            if not isinstance(constraint, UniqueConstraint) or (
                constraint.condition is not None
                or field.model_field_name in constraint.include
                or self.table_rebuild.constraint_references_field(constraint, field)
            ):
                return False
        return True

    async def alter_field(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_name: str,
        candidate_referencing_models: Sequence[type[Model]] = (),
    ) -> None:
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        if self.column_type_changes.keeps_table(old_field, new_field, self.client.dialect):
            # The table stays as it is - only the column's own index and its stored values may change.
            if not self.collect_sql:
                await self.column_conversion_check.check_kept_values(
                    self.qualify_table_name(new_model._meta.db_table, new_model._meta.schema),
                    new_field,
                    self.quote(self.table_rebuild.get_remake_column_name(new_field)),
                )
            old_indexed = old_field.index and not old_field.pk
            new_indexed = new_field.index and not new_field.pk
            if old_indexed != new_indexed:
                # A relation's own index is over its key fields - all of them for a composite key.
                index_fields = (
                    new_field.source_fields
                    if isinstance(new_field, ForeignKeyFieldInstance)
                    else (self.table_rebuild.get_remake_column_name(new_field),)
                )
                index = Index(fields=tuple(index_fields))
                # A Meta.indexes entry under the same name holds the index - it stays as it is.
                if not self.index_statements.is_declared_by_another_entry(
                    new_model, index, model_declares_index=new_indexed
                ):
                    if new_indexed:
                        await self.add_index(new_model, index)
                    else:
                        await self.remove_index(new_model, index)
            await self.column_backfill.rewrite_stored_values(new_model, old_field, new_field)
            return
        await super().alter_field(old_model, new_model, field_name, candidate_referencing_models)

    async def alter_column(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        # A change to a generated field is validated here, as the base editor does.
        await self._alter_generated_field(model, old_field, new_field)
        old_db_field = self.table_rebuild.get_remake_column_name(old_field)
        new_db_field = self.table_rebuild.get_remake_column_name(new_field)

        if old_db_field != new_db_field and not isinstance(old_field, ForeignKeyFieldInstance):
            # An index kept in a table of its own reads its table's columns by name, and is named
            # after them - the one over the renamed column is dropped under its old name first and
            # created again.
            renamed_column_indexes = [
                index for index in self.get_separate_table_indexes(model) if new_field.model_field_name in index.fields
            ]
            for index in renamed_column_indexes:
                old_columns = [
                    old_db_field if column == new_db_field else column
                    for column in model._meta.get_column_names(index.fields)
                ]
                await self.run_sql(
                    "\n".join(index.get_drop_sqls(self, model._meta.db_table, old_columns, model._meta.schema))
                )
            # RENAME COLUMN also rewrites the other tables' FOREIGN KEY clauses naming the column;
            # a table rebuild alone would leave them pointing at a column that no longer exists.
            qualified_table = self.qualify_table_name(model._meta.db_table, model._meta.schema)
            await self.run_sql(
                self.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table, old_column=self.quote(old_db_field), new_column=self.quote(new_db_field)
                )
            )
            if self.column_type_changes.only_renames_column(old_field, new_field, self.client.dialect):
                for index in renamed_column_indexes:
                    await self.add_index(model, index)
                return
            old_field = copy(old_field)
            old_field.source_field = new_db_field

        if not self.collect_sql:
            # SQLite keeps a value the new column type can't hold - a 30-character string in a
            # VARCHAR(5), a fraction in an INTEGER, text no number reads - so the data is checked
            # first.
            qualified_table = self.qualify_table_name(model._meta.db_table, model._meta.schema)
            await self.column_conversion_check.check_alter_field_conversion(
                qualified_table, old_field, new_field, self.quote(new_db_field)
            )
            await self.column_narrowing_check.check_alter_field_narrowing_data_loss(
                qualified_table, old_field, new_field, self.quote(new_db_field)
            )
        await self.table_rebuild.remake_table(model, model, altered_fields=[(old_field, new_field)])
