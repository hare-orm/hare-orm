from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.contrib.test.databases.model_truncation import truncate_all_models
from hare.contrib.test.exceptions import RollbackIsolationEnd
from hare.core.hare_context import HareContext
from hare.core.routing import Routing

if TYPE_CHECKING:  # pragma: nocoverage
    from contextvars import Token
    from types import TracebackType

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext


class RollbackIsolation:
    """Isolates a block - a test - from every other: whatever it writes is thrown away when it ends.
    Each connection of the context runs the block in a transaction of its own that is rolled back
    at the end; a connection whose database has no transactions has its tables emptied instead
    (``truncate_all_models()``). The context is the current one inside the block.

    A transaction the block opens is a savepoint of the isolating one, so a test sees what it
    wrote. The isolating transaction never commits - committing it by hand raises
    ``TransactionManagementError`` - so ``on_commit()`` callbacks registered in the block never
    run by themselves; ``capture_on_commit()`` collects them and runs them on request.

    Example::

        @pytest_asyncio.fixture(scope="module")
        async def database():
            async with hare_test_context(["myapp.models"]) as context:
                yield context


        @pytest_asyncio.fixture
        async def db(database):
            async with RollbackIsolation(database) as context:
                yield context

    Args:
        context: The context isolated - the current one when None.
    """

    def __init__(self, context: HareContext | None = None) -> None:
        self.context = context
        self.isolating_transactions: list[tuple[TransactionContext, TransactionClient]] = []
        self.truncated_connections: list[DatabaseClient] = []
        self.context_token: Token[HareContext | None] | None = None

    async def __aenter__(self) -> HareContext:
        context = self.context or HareContext.require_current()
        self.context_token = HareContext.current_context.set(context)
        # A test starts with none of the writes of the tests before it sending its reads anywhere.
        Routing.forget_writes()
        try:
            for connection in context.connections.all():
                if not connection.features.supports_transactions:
                    self.truncated_connections.append(connection)
                    continue
                transaction_context = connection._in_transaction()
                transaction_client = await transaction_context.__aenter__()
                transaction_client._commit_refusal = (
                    f"The transaction isolating a test on {connection.connection_alias!r} can't be committed - "
                    "everything the test writes is rolled back when it ends. Open a transaction of your own "
                    "with Transactions.atomic() (a savepoint of the isolating one) instead."
                )
                self.isolating_transactions.append((transaction_context, transaction_client))
        except BaseException:
            await self.end(context)
            raise
        return context

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        await self.end(self.context or HareContext.require_current())

    async def end(self, context: HareContext) -> None:
        """Rolls every isolating transaction back, empties the tables of the connections without
        transactions and restores the current context.

        Args:
            context: The isolated context.
        """
        try:
            while self.isolating_transactions:
                transaction_context, _transaction_client = self.isolating_transactions.pop()
                await transaction_context.__aexit__(RollbackIsolationEnd, RollbackIsolationEnd(), None)
            if self.truncated_connections:
                truncated_connections, self.truncated_connections = self.truncated_connections, []
                await truncate_all_models(context=context, connections=truncated_connections)
        finally:
            if self.context_token is not None:
                context_token, self.context_token = self.context_token, None
                HareContext.current_context.reset(context_token)

    @asynccontextmanager
    async def capture_on_commit(self, *, execute: bool = False) -> AsyncGenerator[list[Callable[[], Any]]]:
        """Collects the ``on_commit()`` callbacks registered in the block on the isolating
        transactions - they would never run, the transactions never commit.

        Args:
            execute: Run them when the block exits, in the order they were registered - and the
                ones they register in turn.

        Returns:
            The context giving the list the callbacks are added to when the block exits.
        """
        marks = [len(client._on_commit_callbacks) for _context, client in self.isolating_transactions]
        callbacks: list[Callable[[], Any]] = []
        yield callbacks
        while True:
            new_callbacks = self.take_callbacks_since(marks)
            if not new_callbacks:
                return
            callbacks.extend(new_callbacks)
            if not execute:
                return
            for callback in new_callbacks:
                result = callback()
                if inspect.isawaitable(result):
                    await result

    def take_callbacks_since(self, marks: list[int]) -> list[Callable[[], Any]]:
        """Takes the ``on_commit()`` callbacks registered on the isolating transactions after the
        marks.

        Args:
            marks: How many callbacks each transaction held - left in place.

        Returns:
            The callbacks taken, in registration order per transaction.
        """
        callbacks: list[Callable[[], Any]] = []
        for (_context, client), mark in zip(self.isolating_transactions, marks, strict=False):
            entries = client._on_commit_callbacks[mark:]
            del client._on_commit_callbacks[mark:]
            callbacks.extend(entry.callback for entry in entries)
        return callbacks
