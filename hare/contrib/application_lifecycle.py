"""The Hare context of an application's lifetime - a web application's or a task worker's."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.hare import Hare
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.config import HareConfig


class ApplicationLifecycle:
    """Opens an application's Hare context when it starts and closes it when it stops.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        transactions: True for a transaction per unit of work - a request, a task - on the
            default connection, the connection aliases for one on each of them, False for none.
        transactions_option: The name the application gives ``transactions``, for its error
            messages.

    Raises:
        ConfigurationError: ``transactions`` is neither a bool nor a sequence of names.
    """

    #: The context an application starting in this process uses instead of opening its own - set only by
    #: the pytest plugin's application fixtures, for the application under test.
    test_context: ClassVar[HareContext | None] = None
    #: The applications running in a test's context - it stays the global fallback until the last one
    #: stops (a web application and a task worker of one test).
    test_context_applications: ClassVar[set[int]] = set()

    def __init__(
        self,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        transactions: bool | Sequence[str] = False,
        transactions_option: str = "transactions",
    ) -> None:
        self.config = config
        self.transactions_option = transactions_option
        self.transaction_connection_names = self.get_transaction_connection_names(transactions, transactions_option)
        self.context: HareContext | None = None
        #: Whether the context is one the application opened - stop() closes only such a one.
        self.owns_context = True

    @staticmethod
    def get_transaction_connection_names(
        transactions: bool | Sequence[str], transactions_option: str = "transactions"
    ) -> tuple[str | None, ...]:
        """The connections a unit of work's transaction runs on.

        Args:
            transactions: True, False, or connection aliases.
            transactions_option: The name the application gives ``transactions``, for the error message.

        Returns:
            ``(None,)`` - the default connection - for True, the names for a sequence, nothing for
            False.

        Raises:
            ConfigurationError: ``transactions`` is neither a bool nor a sequence of names - a text, whose
                letters would be read as names, included.
        """
        if isinstance(transactions, bool):
            return (None,) if transactions else ()
        if (
            isinstance(transactions, str)
            or not isinstance(transactions, Sequence)
            or not all(isinstance(name, str) and name for name in transactions)
        ):
            raise ConfigurationError(
                f"{transactions_option} is True, False or a sequence of connection names, got {transactions!r}"
            )
        return tuple(transactions)

    def check_application(self) -> None:
        """Checks what the application declares once the context is set up - nothing by default.

        Raises:
            ConfigurationError: A declaration is wrong.
        """

    async def start(self) -> None:
        """Opens the Hare context as the global fallback - not as the calling task's own context,
        so the units of work an application runs in tasks of their own see it and another task may
        close it - and checks the transactions' connections and the application
        (``check_application()``).

        Under the pytest plugin's ``hare_app_db``/``hare_app_transactional_db`` the application takes the
        test's context (``test_context``) as the global fallback instead of opening its own - its
        configuration's databases are never opened.

        Raises:
            ConfigurationError: A transaction names a connection the configuration lacks, the
                application's check fails, or the context is open already.
        """
        if self.context is not None:
            raise ConfigurationError("The Hare context of the application is open already")
        if ApplicationLifecycle.test_context is not None:
            await self.start_in_test_context(ApplicationLifecycle.test_context)
            return
        self.owns_context = True
        self.context = HareContext()
        try:
            # Current only while it is set up - a task that opens a context as its own must also
            # be the one that leaves it.
            with self.context:
                await self.context.init(config=self.config, _enable_global_fallback=True)
                aliases = self.context.connections.aliases()
                for connection_alias in self.transaction_connection_names:
                    if connection_alias is not None and connection_alias not in aliases:
                        raise ConfigurationError(
                            f"{self.transactions_option} names the connection {connection_alias!r}, configured "
                            f"are: {aliases}"
                        )
                    await self.raise_if_without_transactions(connection_alias)
                self.check_application()
        except BaseException:
            await self.stop()
            raise

    async def start_in_test_context(self, context: HareContext) -> None:
        """Takes a test's context as the application's - the global fallback, which the tasks and threads
        without a context of their own see (a test client's requests), as a context the application
        opened is - and checks the transactions' connections and the application.

        Args:
            context: The test's context.

        Raises:
            ConfigurationError: A transaction names a connection the test's context lacks, the
                application's check fails, or another context is the global fallback.
        """
        HareContext.set_global(context)
        self.context = context
        self.owns_context = False
        ApplicationLifecycle.test_context_applications.add(id(self))
        # Current while it is checked - the test has it open already, so it isn't entered again.
        token = HareContext.current_context.set(context)
        try:
            aliases = context.connections.aliases()
            for connection_alias in self.transaction_connection_names:
                if connection_alias is not None and connection_alias not in aliases:
                    raise ConfigurationError(
                        f"{self.transactions_option} names the connection {connection_alias!r}, the test's "
                        f"are: {aliases}"
                    )
                await self.raise_if_without_transactions(connection_alias)
            self.check_application()
        except BaseException:
            await self.stop()
            raise
        finally:
            HareContext.current_context.reset(token)

    async def raise_if_without_transactions(self, connection_alias: str | None) -> None:
        """Refuses a connection the transactions are asked of that runs none - its features say so
        once it is open - at startup, not by every request. A database that doesn't answer yet is
        left to the first request.

        Args:
            connection_alias: The connection, None for the only one.

        Raises:
            ConfigurationError: The connection runs no transactions.
        """
        connections = cast("HareContext", self.context).connections
        client = connections.get(connection_alias) if connection_alias is not None else connections.get_single_client()
        if client is None or client.features.supports_transactions:
            return
        # A connection learns some of its features once it is open.
        if not await client.ping() or client.features.supports_transactions:
            return
        raise ConfigurationError(
            f"{self.transactions_option} runs a transaction on the connection {client.connection_alias!r}, whose "
            f"database runs none - turn them on in the connection's settings, or leave {self.transactions_option} off"
        )

    async def stop(self) -> None:
        """Closes the connections - waiting for the background observers still running -
        and the Hare context. Does nothing when it isn't open; a test's context is only given back -
        the test closes it."""
        context, self.context = self.context, None
        if context is None:
            return
        if not self.owns_context:
            ApplicationLifecycle.test_context_applications.discard(id(self))
            if not ApplicationLifecycle.test_context_applications:
                HareContext.clear_global_if(context)
            return
        try:
            # Current while it closes - the calling task may have a context of its own, which
            # stays open.
            with context:
                await Hare.close_connections()
        finally:
            await context.close_connections()

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None]:
        """The Hare context for the application's lifetime, as a context manager.

        Raises:
            ConfigurationError: See ``start()``.
        """
        await self.start()
        try:
            yield
        finally:
            await self.stop()
