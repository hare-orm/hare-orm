from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from functools import partial
from types import TracebackType
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.transactions.messages import TRANSACTION_GROUP_CLOSE_FAILED
from hare.transactions.transaction_group_exit import TransactionGroupExit
from hare.transactions.transactions import Transactions

if TYPE_CHECKING:
    from hare.transactions.atomic.atomic import Atomic


class TransactionGroup:
    """Open a transaction per connection and release all of them before returning.

    Connections commit in reverse acquisition order. A failed commit or cancellation
    rolls back those not yet closing; already committed connections are not undone.
    This is a resource lifetime boundary, not a distributed atomic commit protocol.
    Use a fresh group for each request or task.
    """

    def __init__(self, connection_aliases: tuple[str | None, ...]) -> None:
        self.connection_aliases = connection_aliases
        self.exit_stack = AsyncExitStack()
        self.close_task: asyncio.Task[None] | None = None
        self.close_reason: BaseException | None = None
        self.exits: list[TransactionGroupExit] = []

    async def begin(self) -> None:
        """Opens a transaction on each connection.

        Raises:
            Exception: Opening one failed - those already opened are rolled back.
        """
        for connection_alias in self.connection_aliases:
            try:
                context = Transactions.atomic(connection_alias)
                await context.__aenter__()
            except BaseException as error:
                await self.close(error)
                raise
            self.exit_stack.push_async_exit(partial(self._exit_connection, context, connection_alias))

    async def close(self, error: BaseException | None) -> None:
        """Drain every exit in reverse order before propagating failure or cancellation.

        Cancellation never abandons an in-flight exit. Connections not yet closing receive
        the cancellation as their rollback reason. Errors are retained per connection in
        ``exits``; a group preserves the original operation error alongside cleanup failures.
        A subsequent close after completion does nothing.
        """
        if self.close_task is not None and self.close_task.done():
            return
        if self.close_reason is None:
            self.close_reason = error
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close_contexts())
        cancellation = None
        try:
            await ResourceCompletion.wait(self.close_task, on_cancel=self._cancel_close)
        except asyncio.CancelledError as cancelled:
            cancellation = cancelled
        failures = [result.error for result in self.exits if result.error is not None]
        if not failures:
            if cancellation is not None:
                raise cancellation
            return
        errors: list[BaseException] = []
        for failure in (error, cancellation, *failures):
            if failure is not None and all(failure is not previous for previous in errors):
                errors.append(failure)
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup(TRANSACTION_GROUP_CLOSE_FAILED, errors)

    def _cancel_close(self, error: asyncio.CancelledError) -> None:
        """Stop later commits without interrupting an exit which has already started."""
        if self.close_reason is None:
            self.close_reason = error

    async def _close_contexts(self) -> None:
        """Let the standard stack unwind every context; expose errors through ``exits``."""
        try:
            await self.exit_stack.aclose()
        except BaseException:
            # Each callback records its failure before AsyncExitStack propagates it to the
            # next exit. The waiter reports all failures after the stack has been drained.
            pass

    async def _exit_connection(
        self,
        context: Atomic,
        connection_alias: str | None,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record the requested action independently of an uncertain database outcome."""
        reason = self.close_reason if self.close_reason is not None else exception
        failure = None
        try:
            await context.__aexit__(
                type(reason) if reason is not None else None,
                reason,
                reason.__traceback__ if reason is not None else None,
            )
        except BaseException as error:
            failure = error
            raise
        finally:
            self.exits.append(TransactionGroupExit(connection_alias, reason is None, failure))
