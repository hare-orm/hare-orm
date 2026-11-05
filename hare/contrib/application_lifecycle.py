"""The Hare context of an application's lifetime - a web application's or a task worker's."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator, Iterable, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.connections.messages import CONNECTION_RELEASE_FAILED
from hare.core.constants import INITIALIZATION_RELEASE_FAILED
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.transactions.atomic.atomic import Atomic

if TYPE_CHECKING:
    from hare.core.config import HareConfig


class ApplicationLifecycle:
    """Opens an application's Hare context when it starts and closes it when it stops.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        context: An initialized context to borrow; None creates a separate application context.
        transactions: True for a transaction per unit of work - a request, a task - on the
            automatically selected connection, aliases for one on each of them, False for none.
            Selection tries the only configured connection, the common connection of registered
            models, then the alias "default"; ambiguous configurations require explicit aliases.
            An iterable's order is captured once when the lifecycle is constructed.
        transactions_option: The name the application gives ``transactions``, for its error
            messages.

    Raises:
        ConfigurationError: ``transactions`` is neither a bool nor an iterable of names.
    """

    active: ClassVar[ContextVar[ApplicationLifecycle | None]] = ContextVar("hare_application_lifecycle", default=None)

    def __init__(
        self,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        transactions: bool | Iterable[str] = False,
        transactions_option: str = "transactions",
        context: HareContext | None = None,
    ) -> None:
        self.config = config
        self.transactions_option = transactions_option
        self.transaction_connection_names = self.get_transaction_connection_names(transactions, transactions_option)
        self.context: HareContext | None = None
        self.provided_context = context
        self.close_created_context = context is None

    @staticmethod
    def get_transaction_connection_names(
        transactions: bool | Iterable[str], transactions_option: str = "transactions"
    ) -> tuple[str | None, ...]:
        """The connections a unit of work's transaction runs on.

        Args:
            transactions: True, False, or connection aliases.
            transactions_option: The name the application gives ``transactions``, for the error message.

        Returns:
            ``(None,)`` - automatic transaction connection selection - for True, the names for an iterable, nothing for
            False.

        Raises:
            ConfigurationError: ``transactions`` is neither a bool nor an iterable of names - a text, whose
                letters would be read as names, included.
        """
        if isinstance(transactions, bool):
            return (None,) if transactions else ()
        if isinstance(transactions, (str, bytes)):
            names = None
        else:
            try:
                names = tuple(transactions)
            except TypeError:
                names = None
        if names is None or not all(isinstance(name, str) and name for name in names):
            raise ConfigurationError(
                f"{transactions_option} is True, False or an iterable of connection names, got {transactions!r}"
            )
        return names

    def check_application(self) -> None:
        """Checks what the application declares once the context is set up - nothing by default.

        Raises:
            ConfigurationError: A declaration is wrong.
        """

    async def start(self) -> None:
        """Initialize the configured context or borrow the explicitly supplied one.

        Startup never publishes a process-global fallback. Request/task adapters enter
        ``scope()`` around their work. A supplied context is neither initialized nor
        closed here; its caller controls its lifetime, including test isolation.

        Raises:
            ConfigurationError: The context is already started or transaction configuration is invalid.
            BaseExceptionGroup: Initialization and context cleanup both fail.
        """
        if self.context is not None:
            raise ConfigurationError("The Hare context of the application is open already")
        self.context = self.provided_context or HareContext()
        try:
            with self.scope():
                if self.close_created_context:
                    await self.context.init(config=self.config, _enable_global_fallback=False)
                aliases = self.context.connections.aliases()
                for connection_alias in self.transaction_connection_names:
                    if connection_alias is not None and connection_alias not in aliases:
                        raise ConfigurationError(
                            f"{self.transactions_option} names the connection {connection_alias!r}, configured "
                            f"are: {aliases}"
                        )
                    await self.raise_if_without_transactions(connection_alias)
                self.check_application()
        except BaseException as error:
            try:
                await self.stop()
            except BaseException as release_error:
                raise BaseExceptionGroup(INITIALIZATION_RELEASE_FAILED, [error, release_error]) from None
            raise

    @contextmanager
    def scope(self) -> Generator[HareContext]:
        """Activate this application's context for one request, task or lifespan callback."""
        context = self.get_context()
        token = HareContext.current_context.set(context)
        application_token = self.active.set(self)
        try:
            yield context
        finally:
            self.active.reset(application_token)
            HareContext.current_context.reset(token)

    def get_context(self) -> HareContext:
        """Return the started context for adapters with separate begin/end hooks."""
        if self.context is None:
            raise ConfigurationError("The Hare context of the application has not been started")
        return self.context

    async def raise_if_without_transactions(self, connection_alias: str | None) -> None:
        """Refuses a connection the transactions are asked of that runs none - its features say so
        once it is open - at startup, not by every request. A database that doesn't answer yet is
        left to the first request.

        Args:
            connection_alias: The connection, None for the transaction's automatic selection.

        Raises:
            ConfigurationError: The connection runs no transactions.
            QueryError: Automatic selection is ambiguous.
        """
        client = Atomic.get_connection(connection_alias)
        if client.features.supports_transactions:
            return
        # A connection learns some of its features once it is open.
        if not await client.ping() or client.features.supports_transactions:
            return
        raise ConfigurationError(
            f"{self.transactions_option} runs a transaction on the connection {client.connection_alias!r}, whose "
            f"database runs none - turn them on in the connection's settings, or leave {self.transactions_option} off"
        )

    async def stop(self) -> None:
        """Release this application's context once; a supplied context remains open."""
        context, self.context = self.context, None
        if context is None or not self.close_created_context:
            return
        token = HareContext.current_context.set(context)
        try:
            await context.close_connections()
        finally:
            HareContext.current_context.reset(token)

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None]:
        """The Hare context for the application's lifetime, as a context manager.

        Raises:
            ConfigurationError: See ``start()``.
            BaseExceptionGroup: Both application work and context cleanup fail.
        """
        await self.start()
        try:
            with self.scope():
                yield
        except BaseException as error:
            try:
                await self.stop()
            except BaseException as release_error:
                raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, [error, release_error]) from None
            raise
        else:
            await self.stop()
