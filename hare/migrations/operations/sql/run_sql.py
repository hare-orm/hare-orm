from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.migrations.operations.base.hare_operation import HareOperation
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RunSQL(HareOperation):
    """Runs raw SQL - a string, a list of strings or a list of ``(sql, params)`` pairs - with optional
    reverse SQL.
    """

    reduces_to_sql = True
    noop = ""

    def __init__(
        self,
        sql,
        reverse_sql=None,
        *,
        atomic: bool | None = None,
    ) -> None:
        self.sql = sql
        self.reverse_sql = reverse_sql
        self.atomic = atomic
        self.reversible = reverse_sql is not None

    def describe(self) -> str:
        return "Run SQL"

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
        await self._run_sql(state_editor, self.sql)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        if self.reverse_sql is None:
            raise NotImplementedError("RunSQL reverse_sql is not set")
        await self._run_sql(state_editor, self.reverse_sql)

    async def _run_sql(self, state_editor: BaseSchemaEditor, sqls) -> None:
        """Execute SQL statements using the schema editor."""
        if isinstance(sqls, (list, tuple)):
            for sql in sqls:
                params = None
                if isinstance(sql, (list, tuple)):
                    elements = len(sql)
                    if elements == 2:
                        sql, params = sql
                    else:
                        raise ConfigurationError(f"Expected a 2-tuple but got {elements}")

                if params:
                    if state_editor.collect_sql:
                        state_editor.collected_sql.append(f"{sql}  -- params: {params!r}")
                    else:
                        await state_editor.client.execute(sql, params)
                else:
                    await state_editor._run_sql(sql)
        elif sqls != RunSQL.noop:
            await state_editor._run_sql(sqls)
