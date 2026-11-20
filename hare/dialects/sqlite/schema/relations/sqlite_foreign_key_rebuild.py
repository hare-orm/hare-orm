from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.relations.foreign_key_rebuild import ForeignKeyRebuild
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model_binding import ModelBinding


class SqliteForeignKeyRebuild(ForeignKeyRebuild):
    """ForeignKeyRebuild as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_referenced_field_change_plan(
        self,
        binding: ModelBinding,
        old_field: Field[Any],
        new_field: Field[Any],
        referencing_key_columns: list[ReferencingKeyColumns],
    ) -> SchemaChangePlan:
        """Rebuilds the altered table, then every table whose key columns reference it - a rebuilt
        table takes its key column types from the referenced columns' current definitions."""
        model = binding._model
        steps = list(self.editor.get_alter_column_plan(binding, old_field, new_field).steps)
        remade_model_keys = {(binding.app, model.__name__)}
        for referencing in referencing_key_columns:
            if isinstance(referencing.relation_field, ManyToManyFieldInstance):
                steps.extend(self.get_many_to_many_rebuild_plan(referencing.binding, referencing.relation_field).steps)
                continue
            referencing_model_key = (referencing.binding.app, referencing.model.__name__)
            if referencing_model_key in remade_model_keys:
                continue
            remade_model_keys.add(referencing_model_key)
            steps.extend(self.editor.table_rebuild.get_rebuild_plan(referencing.binding, referencing.binding).steps)
        return SchemaChangePlan(tuple(steps))

    def get_composite_index_change_plan(
        self, binding: ModelBinding, old_field: Field[Any], new_field: Field[Any], *, index_dropped: bool = False
    ) -> SchemaChangePlan:
        """The column rebuild already includes the target indexes."""
        return SchemaChangePlan(())

    def get_relation_foreign_key_restore_plan(
        self, binding: ModelBinding, relation_field: Field[Any]
    ) -> SchemaChangePlan:
        """The key-column rebuild already restored its foreign keys."""
        return SchemaChangePlan(())

    def get_foreign_key_rebuild_plan(
        self, binding: ModelBinding, field: ForeignKeyFieldInstance[Model]
    ) -> SchemaChangePlan:
        """A changed foreign key is recreated with its table."""
        return self.editor.table_rebuild.get_rebuild_plan(binding, binding)

    def get_many_to_many_rebuild_plan(
        self,
        binding: ModelBinding,
        field: ManyToManyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Rebuilds the auto-managed through table from the field's current metadata, keeping
        its rows."""
        schema = binding.schema
        rebuilt_field = copy(field)
        rebuilt_field.through = self.editor.table_rebuild.get_rebuilt_table_name(field.through)
        # The indexes are created after the rename below, so they get the real table's name.
        rebuilt_field.unique = False
        rebuilt_field.index = False
        definition = self.editor.table_creation.get_many_to_many_table_definition(binding, rebuilt_field)
        if definition is None:
            return SchemaChangePlan(())
        steps: list[SchemaStep] = [SchemaStatement(definition)]
        qualified_old = self.editor.qualify_table_name(field.through, schema)
        qualified_new = self.editor.qualify_table_name(rebuilt_field.through, schema)
        columns = ", ".join(self.editor.quote(key) for key in (*field.backward_keys, *field.forward_keys))
        # Both table names and every column name are dialect-quoted identifiers.
        steps.append(
            SchemaStatement(
                f"INSERT INTO {qualified_new} ({columns}) SELECT {columns} FROM {qualified_old}",  # nosec B608
                frozenset({SchemaEffect.REWRITE_ROWS}),
            )
        )
        steps.extend(
            self.editor.table_rebuild.get_replace_plan(field.through, rebuilt_field.through, schema, fields=()).steps
        )
        if field.unique:
            steps.append(
                SchemaStatement(
                    self.editor.index_statements.get_unique_index_sql(
                        field.through, [*field.backward_keys, *field.forward_keys], schema=schema
                    )
                )
            )
        for index_keys in field.get_through_index_keys():
            steps.append(
                SchemaStatement(
                    self.editor.index_statements.get_table_index_sql(field.through, index_keys, schema=schema)
                )
            )
        return SchemaChangePlan(tuple(steps))
