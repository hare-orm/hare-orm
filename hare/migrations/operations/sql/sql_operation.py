from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.migrations.operations.base.operation import Operation
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class SQLOperation(Operation):
    """Runs one raw SQL statement, optionally with bind parameters. Leaves the migration state
    untouched and cannot be unapplied."""

    reversible = False

    def __init__(self, query: str, values: list[Any]):
        self.query = query
        self.values = values

    def describe(self) -> str:
        return "Raw SQL operation"

    async def run(
        self,
        app_label: str,
        state: State,
        dry_run: bool,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not dry_run:
            await self.database_forward(app_label, state, state, state_editor)

    def state_forward(self, app_label: str, state: State) -> None:
        return None

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if state_editor is None:
            return
        if not self.values:
            await state_editor._run_sql(self.query)
        elif state_editor.collect_sql:
            state_editor.collected_sql.append(f"{self.query}  -- params: {self.values!r}")
        else:
            await state_editor.client.execute_dicts(self.query, self.values)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        raise NotImplementedError("SQLOperation is not reversible - use RunSQL with reverse_sql instead.")
