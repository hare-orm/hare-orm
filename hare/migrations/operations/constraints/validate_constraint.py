from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class ValidateConstraint(ModelBoundOperation):
    """Checks the existing rows against a constraint added ``NOT VALID`` (Postgres ``VALIDATE
    CONSTRAINT``) - with a lock that doesn't block writes. Nothing to do on SQLite, or going back.

    Args:
        model_name: The constrained model.
        name: The constraint's name.
    """

    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

    def describe(self) -> str:
        return f"Validate constraint {self.name} on {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        pass

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await state_editor.constraint_statements.validate_constraint(self._model(new_state, app_label), self.name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        pass
