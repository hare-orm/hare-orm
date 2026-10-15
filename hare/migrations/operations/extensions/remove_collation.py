from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.extensions.create_collation import CreateCollation
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RemoveCollation(CreateCollation):
    """Drops a Postgres collation - given the arguments it was created with, to create it again going
    back."""

    def describe(self) -> str:
        return f"Remove collation {self.name}"

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._drop(state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._create(state_editor)
