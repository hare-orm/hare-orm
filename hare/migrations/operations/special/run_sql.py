from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.query.expressions.raw_sql import RawSQL
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer

if TYPE_CHECKING:
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
    backward_reads_state_left = False
    backward_reads_state_reached = False

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
        """Runs the statements through the schema editor."""
        if isinstance(sqls, (list, tuple)):
            for sql in sqls:
                parameters: Any = None
                takes_parameters = False
                if isinstance(sql, (list, tuple)):
                    elements = len(sql)
                    if elements == 2:
                        sql, parameters = sql
                        takes_parameters = True
                    else:
                        raise ConfigurationError(f"Expected a 2-tuple but got {elements}")

                if takes_parameters:
                    # A (sql, parameters) pair, an empty list too: %s placeholders on every database, as raw()
                    # takes them - written in the connection's own style, and with the values in the text where
                    # the SQL is only shown.
                    raw_sql = RawSQL(sql, parameters)
                    sql_context = state_editor.client.query_class.SQL_CONTEXT
                    if state_editor.collect_sql:
                        state_editor.collected_sql.append(raw_sql.get_sql(sql_context))
                    else:
                        parameterizer = RecordingParameterizer()
                        rendered_sql = raw_sql.get_sql(sql_context.copy(parameterizer=parameterizer))
                        await state_editor.run_parameterized_sql(rendered_sql, parameterizer.values)
                else:
                    await state_editor.run_raw_sql(sql)
        elif isinstance(sqls, str) and sqls != RunSQL.noop:
            await state_editor.run_raw_sql(sqls)
