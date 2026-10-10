from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.operations.models.model_table_operation import ModelTableOperation
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class DeleteModel(ModelTableOperation, HareOperation):
    """Deletes a model and drops its table."""

    def __init__(self, name: str, state_only: bool = False) -> None:
        """
        Args:
            name: The model's class name.
            state_only: Only removes the model from the migration state and keeps its table, e.g.
                for a model moved to another app.
        """
        self.name = name
        self.state_only = state_only

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The model deleted."""
        return frozenset({self.get_model_label(self.name, app_label)})

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def describe(self) -> str:
        if self.state_only:
            return f"Delete model {self.name} (state only, its table is kept)"
        return f"Delete model {self.name}"

    def get_effect(self, app_label: str, state: State, dialect: Dialect) -> OperationEffect:
        if self.state_only:
            return OperationEffect(operation=self, reversible=self.reversible)
        return OperationEffect(
            operation=self,
            reversible=self.reversible,
            loses_data=True,
            reason=f"drops the table of {self.name} with its rows",
        )

    def state_forward(self, app_label: str, state: State) -> None:
        model_reference = f"{app_label}.{self.name}"
        model_key = (app_label, self.name)

        for state_key, model_state in state.models.items():
            if state_key == model_key:
                # The model's own self-referencing relation doesn't keep it.
                continue
            for field_name, field in model_state.fields.items():
                if not isinstance(field, DIRECT_RELATION_FIELDS):
                    continue

                if field.model_name == model_reference:
                    raise IncompatibleStateError(
                        f"{model_reference} is still referenced from {model_state.app}.{model_state.name} "
                        f"(field {field_name!r}) - the migration removing or repointing that field must "
                        f"run before the one deleting {model_reference}."
                    )

        model_state_to_delete = state.models.pop((app_label, self.name), None)
        if not model_state_to_delete:
            raise IncompatibleStateError()

        models_to_reload = set()

        for field in model_state_to_delete.fields.values():
            if not isinstance(field, DIRECT_RELATION_FIELDS):
                continue

            models_to_reload.add(state.apps.split_reference(field.model_name))

        state.apps.unregister_model(app_label, self.name)
        state.reload_models(models_to_reload)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._delete_model_table(app_label, old_state, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._create_model_table(app_label, new_state, state_editor)
