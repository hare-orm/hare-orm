from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.models.table_renaming_operation import TableRenamingOperation
from hare.migrations.state.project.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RenameModel(TableRenamingOperation):
    def __init__(self, old_name: str, new_name: str) -> None:
        self.old_name = old_name
        self.new_name = new_name

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.old_name, self.new_name)

    def describe(self) -> str:
        return f"Rename model {self.old_name} to {self.new_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        if (app_label, self.old_name) not in state.models:
            raise IncompatibleStateError()
        # The models the old class reaches (e.g. the target of its FK/M2M) still carry the backward
        # relation it registered on them - they're re-rendered below, or the renamed class
        # registering the same backward relation again collides with that stale one.
        related_model_keys = state._find_related_models(app_label, self.old_name) - {(app_label, self.old_name)}
        model_state_to_change = state.models.pop((app_label, self.old_name))

        state.apps.unregister_model(app_label, self.old_name)

        old_table = model_state_to_change.table
        model_state_to_change.name = self.new_name
        table_is_explicit = model_state_to_change.options.get(ModelOption.TABLE_IS_EXPLICIT)
        if table_is_explicit is None:
            # A migration recorded before table_is_explicit existed.
            table_was_auto_derived = old_table == self.old_name.lower()
        else:
            table_was_auto_derived = not table_is_explicit
        if table_was_auto_derived:
            model_state_to_change.table = self.new_name.lower()
            if model_state_to_change.options.get(ModelOption.TABLE) == old_table:
                model_state_to_change.options[ModelOption.TABLE] = model_state_to_change.table
        state.models[(app_label, self.new_name)] = model_state_to_change
        old_model_reference = f"{app_label}.{self.old_name}"
        new_model_reference = f"{app_label}.{self.new_name}"
        models_to_reload = {(app_label, self.new_name)} | (related_model_keys & state.models.keys())

        for model_key, model_state in state.models.items():
            for field_name, field in model_state.fields.items():
                if not isinstance(
                    field,
                    (
                        ForeignKeyFieldInstance,
                        OneToOneFieldInstance,
                        ManyToManyFieldInstance,
                    ),
                ):
                    continue

                if field.model_name == old_model_reference:
                    new_field = deepcopy(field)
                    new_field.model_name = new_model_reference
                    model_state.fields[field_name] = new_field
                    models_to_reload.add(model_key)

        # Every model whose relation now points at the new name is re-rendered too - its
        # relation fields (and any automatic M2M through-table names derived from the target's
        # name) would otherwise keep resolving against the unregistered old model class.
        state.reload_models(models_to_reload)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_table_rename(app_label, old_state, new_state, state_editor, self.old_name, self.new_name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_table_rename(app_label, old_state, new_state, state_editor, self.new_name, self.old_name)
