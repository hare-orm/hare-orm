from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RunPython(HareOperation):
    """Runs a function on the migration's models and schema editor.

    Args:
        code: Called with the state's apps and the schema editor when the migration is applied.
        reverse_code: Called the same way when it's unapplied - without one, the operation can't be.
        atomic: Run in a transaction of its own; None follows the migration.
        elidable: Leave the operation out when its migration is squashed - for data a squashed
            history has no need to write again.
        tenant_schema: Run in each tenant's schema instead of the shared one, on a connection with
            ``tenant_schema_template``.
    """

    reduces_to_sql = False

    def __init__(
        self,
        code: Callable[..., Any],
        reverse_code: Callable[..., Any] | None = None,
        *,
        atomic: bool | None = None,
        elidable: bool = False,
        tenant_schema: bool = False,
    ) -> None:
        if not callable(code):
            raise TypeError("RunPython must be supplied with a callable")
        if reverse_code is not None and not callable(reverse_code):
            raise TypeError("RunPython must be supplied with callable arguments")
        self.code = code
        self.reverse_code = reverse_code
        self.atomic = atomic
        self.elidable = elidable
        self.tenant_schema = tenant_schema
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
