from __future__ import annotations

from typing import TYPE_CHECKING

from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterModelSchema(HareOperation):
    """Moves a model's table to another schema (``Meta.schema``), keeping its data - the partitions of a
    partitioned table, its automatic through tables and the objects it declares in its schema (views,
    materialized views, functions, sequences, the functions of its triggers) with it. PostgreSQL moves
    them with ``ALTER ... SET SCHEMA``; a database whose schemas are the databases of its server moves a
    table with ``RENAME TABLE``, refusing a model whose objects name the table by its database; SQLite
    can't move a table to another attached database and refuses the move. A refusal comes before any
    statement (``Schemas.raise_if_model_unmovable()``). ``CreateSchema`` creates the schema itself.
    """

    def __init__(self, name: str, schema: str | None) -> None:
        self.name = name
        self.schema = schema

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The model moved."""
        return frozenset({self.get_model_label(self.name, app_label)})

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def describe(self) -> str:
        return f"Move table for {self.name} to schema {self.schema!r}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.name)
        if self.schema:
            model_state.options[ModelOption.SCHEMA] = self.schema
        else:
            model_state.options.pop(ModelOption.SCHEMA, None)
        state.mark_models_changed([(app_label, self.name)])

    async def _apply_move_schema(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None,
    ) -> None:
        if not state_editor:
            return
        old_model = old_state.apps.get_model(f"{app_label}.{self.name}")
        new_model = new_state.apps.get_model(f"{app_label}.{self.name}")
        if (old_model._meta.schema or None) == (new_model._meta.schema or None):
            return
        state_editor.schemas.raise_if_model_unmovable(old_model)
        await state_editor.schemas.move_table_to_schema(
            new_model._meta.db_table, old_model._meta.schema, new_model._meta.schema
        )
        # A partition is a table of its own, left where it is by the move of the table it is of.
        for partition_table_name in state_editor.table_partitions.get_partition_table_names(old_model):
            await state_editor.schemas.move_table_to_schema(
                partition_table_name, old_model._meta.schema, new_model._meta.schema
            )
        # An automatic M2M through table lives in the schema of the model declaring the relation.
        for field_name in sorted(new_model._meta.many_to_many_fields):
            old_field = old_model._meta.fields_map.get(field_name)
            new_field = new_model._meta.fields_map[field_name]
            if (
                not isinstance(old_field, ManyToManyFieldInstance)
                or not isinstance(new_field, ManyToManyFieldInstance)
                or old_field._generated
                or old_field.through_model is not None
                or new_field.through_model is not None
            ):
                continue
            await state_editor.schemas.move_table_to_schema(
                old_field.through, old_field.through_schema, new_field.through_schema
            )
        old_schema, new_schema = old_model._meta.schema, new_model._meta.schema
        # The objects the model declares live in its schema - each dialect part moves its own.
        old_meta = old_model._meta
        for view in old_meta.views:
            await state_editor.views.move_to_schema(old_model, view, old_schema, new_schema)
        for materialized_view in old_meta.materialized_views:
            await state_editor.materialized_views.move_to_schema(old_model, materialized_view, old_schema, new_schema)
        for function in old_meta.functions:
            await state_editor.database_functions.move_to_schema(old_model, function, old_schema, new_schema)
        for sequence in old_meta.sequences:
            await state_editor.sequences.move_to_schema(old_model, sequence, old_schema, new_schema)
        for trigger in old_meta.triggers:
            await state_editor.trigger_statements.move_trigger_to_schema(old_model, trigger, old_schema, new_schema)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_move_schema(app_label, old_state, new_state, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        # The states come swapped for a backward run, as to every operation - the move goes back.
        await self._apply_move_schema(app_label, old_state, new_state, state_editor)
