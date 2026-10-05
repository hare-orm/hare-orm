from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RunSQL(HareOperation):
    """Runs raw SQL - a string, a list of strings or a list of ``(sql, params)`` pairs - with optional
    reverse SQL.

    Args:
        sql: The SQL run when the migration is applied.
        reverse_sql: The SQL run when it's unapplied - without it, the operation can't be.
        atomic: Run in a transaction of its own; None follows the migration.
        elidable: Leave the operation out when its migration is squashed.
        tenant_schema: Run in each tenant's schema instead of the shared one, on a connection with
            ``tenant_schema_template``.
    """

    reduces_to_sql = True
    noop = ""

    def __init__(
        self,
        sql: str | Sequence[Any],
        reverse_sql: str | Sequence[Any] | None = None,
        *,
        atomic: bool | None = None,
        elidable: bool = False,
        tenant_schema: bool = False,
    ) -> None:
        self.sql = sql
        self.reverse_sql = reverse_sql
        self.atomic = atomic
        self.elidable = elidable
        self.tenant_schema = tenant_schema
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
        await self.run_sql(state_editor, self.sql)

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
        await self.run_sql(state_editor, self.reverse_sql)

    async def run_sql(self, state_editor: BaseSchemaEditor, sqls: str | Sequence[Any] | None) -> None:
        """Execute SQL statements using the schema editor."""
        if isinstance(sqls, (list, tuple)):
            for sql in sqls:
                parameters = None
                if isinstance(sql, (list, tuple)):
                    elements = len(sql)
                    if elements == 2:
                        sql, parameters = sql
                    else:
                        raise ConfigurationError(f"Expected a 2-tuple but got {elements}")

                if parameters:
                    if state_editor.collect_sql:
                        state_editor.collected_sql.append(f"{sql}  -- params: {parameters!r}")
                    else:
                        await state_editor.client.execute(sql, parameters)
                else:
                    await state_editor.run_sql(sql)
        elif isinstance(sqls, str) and sqls != RunSQL.noop:
            await state_editor.run_sql(sqls)
