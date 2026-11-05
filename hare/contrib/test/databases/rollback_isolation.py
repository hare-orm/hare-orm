from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.contrib.test.databases.model_truncation import truncate_all_models
from hare.contrib.test.databases.resource_cleanup import ResourceCleanup
from hare.contrib.test.exceptions import RollbackIsolationEnd
from hare.contrib.test.messages import ROLLBACK_ISOLATION_ACTIVE
from hare.core.hare_context import HareContext
from hare.core.routing import Routing
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from contextvars import Token
    from types import TracebackType

    from hare.dialects.base.client.transaction_client import TransactionClient


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
        self.isolating_clients: list[TransactionClient] = []
        self.context_token: Token[HareContext | None] | None = None
        self.cleanup = ResourceCleanup()

    async def __aenter__(self) -> HareContext:
        if self.context_token is not None:
            raise ConfigurationError(ROLLBACK_ISOLATION_ACTIVE)
        self.cleanup = ResourceCleanup()
        context = self.context or HareContext.require_current()
        self.context_token = HareContext.current_context.set(context)
        # A test starts with none of the writes of the tests before it sending its reads anywhere.
        Routing.forget_writes()
        try:
            for connection in context.connections.all():
                if not connection.features.supports_transactions:
                    self.cleanup.push(truncate_all_models, context=context, connections=[connection])
                    continue
                transaction_context = connection._in_transaction()
                transaction_client = await transaction_context.__aenter__()
                self.cleanup.push(transaction_context.__aexit__, RollbackIsolationEnd, RollbackIsolationEnd(), None)
                transaction_client._commit_refusal = (
                    f"The transaction isolating a test on {connection.connection_alias!r} can't be committed - "
                    "everything the test writes is rolled back when it ends. Open a transaction of your own "
                    "with Transactions.atomic() (a savepoint of the isolating one) instead."
                )
                self.isolating_clients.append(transaction_client)
        except BaseException as error:
            await self.end(error=error)
            raise
        return context

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        await self.end(error=exception)

    async def end(self, *, error: BaseException | None = None) -> None:
        """Rolls every isolating transaction back, empties the tables of the connections without
        transactions and restores the current context.

        Args:
            error: The original body or startup error, retained if cleanup also fails.

        Every release finishes even when another fails or the caller is cancelled.
        The context token is restored in the task that entered the isolation.
        """
        try:
            await self.cleanup.close()
        finally:
            self.isolating_clients.clear()
            if self.context_token is not None:
                context_token, self.context_token = self.context_token, None
                HareContext.current_context.reset(context_token)
        self.cleanup.raise_errors(error)

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
        async with TransactionCallbacks.capture_on_commit(self.isolating_clients, execute=execute) as callbacks:
            yield callbacks
