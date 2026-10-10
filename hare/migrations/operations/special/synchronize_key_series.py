from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.model_bound_operation import ModelBoundOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class SynchronizeKeySeries(ModelBoundOperation):
    """Moves the series a model's generated keys come from past the greatest key of its table - after
    rows were written with keys of their own (an import, a copy from another database). Changes no
    state; going back does nothing. On a database generating keys as it writes the rows it does
    nothing.

    Args:
        model_name: The model.
    """

    reads_old_state = False

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def describe(self) -> str:
        return f"Synchronize the key series of {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        return None

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if state_editor is None or state_editor.collect_sql:
            return
        await state_editor.client.synchronize_key_series(self._model(new_state, app_label))

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        return None
