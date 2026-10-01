from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from hare.migrations.operations.base.hare_operation import HareOperation
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RunPython(HareOperation):
    reduces_to_sql = False

    def __init__(
        self,
        code,
        reverse_code=None,
        *,
        atomic: bool | None = None,
    ) -> None:
        if not callable(code):
            raise TypeError("RunPython must be supplied with a callable")
        if reverse_code is not None and not callable(reverse_code):
            raise TypeError("RunPython must be supplied with callable arguments")
        self.code = code
        self.reverse_code = reverse_code
        self.atomic = atomic
        self.reversible = reverse_code is not None

    def describe(self) -> str:
        return "Run Python code"

    def state_forward(self, app_label: str, state: State) -> None:
        return None

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        result = self.code(old_state.apps, state_editor)
        if inspect.isawaitable(result):
            await result

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        if self.reverse_code is None:
            raise NotImplementedError("RunPython reverse_code is not set")
        result = self.reverse_code(old_state.apps, state_editor)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def noop(apps: StateApps, schema_editor: BaseSchemaEditor) -> None:
        return None
