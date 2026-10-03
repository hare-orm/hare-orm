from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.base.hare_operation import HareOperation
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RemoveExtension(HareOperation):
    """Drop a Postgres extension after every table that depends on it is gone."""

    def __init__(self, extension_name: str) -> None:
        self.extension_name = extension_name

    def describe(self) -> str:
        return f"Remove extension {self.extension_name}"

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_extension(self.extension_name, False, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_extension(self.extension_name, True, state_editor)
