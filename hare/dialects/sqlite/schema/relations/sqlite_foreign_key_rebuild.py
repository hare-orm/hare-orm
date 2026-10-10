from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.relations.foreign_key_rebuild import ForeignKeyRebuild
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteForeignKeyRebuild(ForeignKeyRebuild):
    """ForeignKeyRebuild as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    async def alter_referenced_field(
        self,
        model: type[Model],
        old_field: Field[Any],
        new_field: Field[Any],
        referencing_key_columns: list[ReferencingKeyColumns],
    ) -> None:
        """Rebuilds the altered table, then every table whose key columns reference it - a rebuilt
        table takes its key column types from the referenced columns' current definitions."""
        await self.editor.alter_column(model, old_field, new_field)
        remade_model_keys = {(model._meta.app, model.__name__)}
        for referencing in referencing_key_columns:
            if isinstance(referencing.relation_field, ManyToManyFieldInstance):
                await self.rebuild_many_to_many_through_foreign_keys(referencing.model, referencing.relation_field)
                continue
            referencing_model_key = (referencing.model._meta.app, referencing.model.__name__)
            if referencing_model_key in remade_model_keys:
                continue
            remade_model_keys.add(referencing_model_key)
            await self.editor.table_rebuild.remake_table(referencing.model)

    async def alter_composite_relation_index(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any]
    ) -> None:
        """Does nothing - altering the key columns already rebuilt the table with the new
        definition's indexes.

        Args:
            model: The model rendered from the target state.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """

    async def restore_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        """No-op - the table rebuild that altered the key column already re-created them."""

    async def rebuild_foreign_key_constraints(
        self, model: type[Model], foreign_key_field: ForeignKeyFieldInstance[Model]
    ) -> None:
        """SQLite can't replace a constraint in place - the table is rebuilt from the model's
        current metadata, which already carries the field's ON DELETE action."""
        await self.editor.table_rebuild.remake_table(model)

    async def rebuild_many_to_many_through_foreign_keys(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> None:
        """Rebuilds the auto-managed through table from the field's current metadata, keeping
        its rows."""
        schema = model._meta.schema
        rebuilt_field = copy(field)
        rebuilt_field.through = f"new__{field.through}"
        # The indexes are created after the rename below, so they get the real table's name.
        rebuilt_field.unique = False
        rebuilt_field.index = False
        definition = self.editor.table_creation.get_many_to_many_table_definition(model, rebuilt_field)
        if definition is None:
            return
        await self.editor.run_sql(definition)
        qualified_old = self.editor.qualify_table_name(field.through, schema)
        qualified_new = self.editor.qualify_table_name(rebuilt_field.through, schema)
        columns = ", ".join(self.editor.quote(key) for key in (*field.backward_keys, *field.forward_keys))
        await self.editor.run_sql(f"INSERT INTO {qualified_new} ({columns}) SELECT {columns} FROM {qualified_old}")  # nosec B608
        await self.editor.table_rebuild.replace_table(field.through, rebuilt_field.through, schema, fields=())
        if field.unique:
            await self.editor.run_sql(
                self.editor.index_statements.get_unique_index_sql(
                    field.through, [*field.backward_keys, *field.forward_keys], schema=schema
                )
            )
        for index_keys in field.get_through_index_keys():
            await self.editor.run_sql(
                self.editor.index_statements.get_table_index_sql(field.through, index_keys, schema=schema)
            )
