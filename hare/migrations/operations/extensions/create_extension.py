from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class CreateExtension(HareOperation):
    """Creates a Postgres extension (``citext``, ``postgis``) before the tables that need it. Not
    tracked in the state - the autodetector derives the extensions from ``Meta.extensions`` and the
    fields.
    """

    def __init__(self, extension_name: str) -> None:
        self.extension_name = extension_name

    def describe(self) -> str:
        return f"Create extension {self.extension_name}"

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_extension(self.extension_name, True, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_extension(self.extension_name, False, state_editor)
