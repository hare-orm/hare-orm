from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import MISSING_MODEL_MESSAGE
from hare.migrations.operations.models.table_renaming_operation import TableRenamingOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RenameModel(TableRenamingOperation):
    """Renames a model and its table."""

    def __init__(self, old_name: str, new_name: str) -> None:
        self.old_name = old_name
        self.new_name = new_name

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The model under both its names."""
        return frozenset(
            {self.get_model_label(self.old_name, app_label), self.get_model_label(self.new_name, app_label)}
        )

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """Renames in a row become one rename - none when it ends at the first name."""
        if isinstance(other, RenameModel) and self.is_same_model_name(self.new_name, other.old_name):
            if self.is_same_model_name(self.old_name, other.new_name):
                return []
            return [RenameModel(self.old_name, other.new_name)]
        return super().reduce(other, app_label)

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.old_name, self.new_name)

    def get_renamed_table(self, table: str, table_is_explicit: bool | None) -> str:
        """The model's table after the rename - named after the new class, unless the model names
        its table itself.

        Args:
            table: The table before the rename.
            table_is_explicit: Whether the model names its table; None in a migration recorded
                before that was kept - the table then counts as given when it differs from the
                class name.

        Returns:
            The table.
        """
        if table_is_explicit is None:
            table_is_explicit = table != self.old_name.lower()
        return table if table_is_explicit else self.new_name.lower()

    def get_field_relating_to_new_name(self, field: Any, app_label: str) -> Any:
        """A relation to the renamed model, or a many-to-many relation through it, as it is after
        the rename.

        Args:
            field: A field of any model.
            app_label: The migration's app.

        Returns:
            A copy naming the model by its new name; the field itself when it doesn't name the model.
        """
        if not isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance, ManyToManyFieldInstance)):
            return field
        old_reference = f"{app_label}.{self.old_name}"
        new_reference = f"{app_label}.{self.new_name}"
        relates_to_model = field.model_name == old_reference
        goes_through_model = isinstance(field, ManyToManyFieldInstance) and field.through_model == old_reference
        if not relates_to_model and not goes_through_model:
            return field
        renamed_field = deepcopy(field)
        if relates_to_model:
            renamed_field.model_name = new_reference
        if isinstance(renamed_field, ManyToManyFieldInstance) and goes_through_model:
            renamed_field.through_model = new_reference
        return renamed_field

    def describe(self) -> str:
        return f"Rename model {self.old_name} to {self.new_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        if (app_label, self.old_name) not in state.models:
            raise IncompatibleStateError(MISSING_MODEL_MESSAGE.format(app_label=app_label, model_name=self.old_name))
        # The models the old class reaches (e.g. the target of its FK/M2M) still carry the backward
        # relation it registered on them - they're re-rendered below, or the renamed class
        # registering the same backward relation again collides with that stale one.
        related_model_keys = state.find_related_models(app_label, self.old_name) - {(app_label, self.old_name)}
        model_state_to_change = state.models.pop((app_label, self.old_name))

        state.apps.unregister_model(app_label, self.old_name)

        old_table = model_state_to_change.table
        model_state_to_change.name = self.new_name
        model_state_to_change.table = self.get_renamed_table(
            old_table, model_state_to_change.options.get(ModelOption.TABLE_IS_EXPLICIT)
        )
        if model_state_to_change.options.get(ModelOption.TABLE) == old_table:
            model_state_to_change.options[ModelOption.TABLE] = model_state_to_change.table
        self.rename_table_in_views(model_state_to_change, old_table, model_state_to_change.table)
        state.models[(app_label, self.new_name)] = model_state_to_change
        models_to_reload = {(app_label, self.new_name)} | (related_model_keys & state.models.keys())

        for model_key, model_state in state.models.items():
            for field_name, field in model_state.fields.items():
                renamed_field = self.get_field_relating_to_new_name(field, app_label)
                if renamed_field is not field:
                    model_state.fields[field_name] = renamed_field
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
