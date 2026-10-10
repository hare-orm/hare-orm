from __future__ import annotations

from typing import TYPE_CHECKING

from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterModelSchema(HareOperation):
    """Moves a model's table to another schema (``Meta.schema``) with ``ALTER TABLE ... SET SCHEMA``,
    keeping its data. A no-op without schemas (SQLite). ``CreateSchema`` creates the schema itself.
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
        state.reload_model(app_label, self.name)

    async def _apply_move_schema(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None,
    ) -> None:
        if not state_editor:
            return
        if not state_editor.client.features.supports_schemas:
            # No schemas on this dialect - a table stays where it is.
            return
        old_model = old_state.apps.get_model(f"{app_label}.{self.name}")
        new_model = new_state.apps.get_model(f"{app_label}.{self.name}")
        await state_editor.schemas.move_table_to_schema(
            new_model._meta.db_table, old_model._meta.schema, new_model._meta.schema
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
        # Same old/new-state convention as AlterModelTable.database_backward above.
        await self._apply_move_schema(app_label, old_state, new_state, state_editor)
