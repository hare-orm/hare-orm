from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from typing import TYPE_CHECKING

from hare.dialects.base.results.statement_result import StatementResult
from hare.query.execution.write_statements import WriteCommand, WriteCopy, WriteMany

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class BoundWrite:
    """The actual ordered commands of one write, prepared before any command is sent.

    Binding, validation and strategy selection are complete. Execution does not read
    model values, call defaults or regroup rows. The caller controls the transaction
    and applies each result to its objects before asking for the next result.
    """

    __slots__ = ("commands",)

    def __init__(self, commands: Iterable[WriteCommand]) -> None:
        self.commands = tuple(commands)

    @property
    def requires_transaction(self) -> bool:
        """Whether the plan performs multiple statements, including executemany rows."""
        return sum(len(command.rows) if isinstance(command, WriteMany) else 1 for command in self.commands) > 1

    async def execute(self, connection: DatabaseClient) -> AsyncGenerator[StatementResult]:
        """Execute commands once in their bound order, yielding even unknown outcomes.

        Drivers that do not report a bulk count produce a ``StatementResult`` with ``row_count=None``,
        never a fabricated zero. A failure stops further commands; the enclosing write owns rollback and
        any recovery needed on a database without transactions.
        """
        for command in self.commands:
            yield await self.execute_command(connection, command)

    @staticmethod
    async def execute_command(connection: DatabaseClient, command: WriteCommand) -> StatementResult:
        """Send one prepared command without consulting the original model values."""
        if isinstance(command, WriteCopy):
            await connection.copy(command.table, command.columns, command.rows, command.column_types, command.schema)
            return StatementResult(None, ())
        if isinstance(command, WriteMany):
            if command.recovery is None:
                await connection.execute_many(command.sql, command.rows)
            else:
                await connection.execute_many(command.sql, command.rows, recovery=command.recovery)
            return StatementResult(None, ())
        if command.recovery is None:
            return await connection.execute(command.sql, command.parameters, returns_rows=command.returns_rows)
        return await connection.execute(
            command.sql, command.parameters, returns_rows=command.returns_rows, recovery=command.recovery
        )
