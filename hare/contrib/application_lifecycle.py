"""The Hare context of an application's lifetime - a web application's or a task worker's."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.core.hare import Hare
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.config import HareConfig


class ApplicationLifecycle:
    """Opens an application's Hare context when it starts and closes it when it stops.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        transactions: True for a transaction per unit of work - a request, a task - on the
            default connection, the connection names for one on each of them, False for none.
        transactions_option: The name the application gives ``transactions``, for its error
            messages.

    Raises:
        ConfigurationError: ``transactions`` is neither a bool nor a sequence of names.
    """

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

    @staticmethod
    def get_transaction_connection_names(
        transactions: bool | Sequence[str], transactions_option: str
    ) -> tuple[str | None, ...]:
        """The connections a unit of work's transaction runs on.

        Args:
            transactions: True, False, or connection names.
            transactions_option: The option's name, for the error messages.

        Returns:
            ``(None,)`` - the default connection - for True, the names for a sequence, nothing for
            False.

        Raises:
            ConfigurationError: ``transactions`` is neither a bool nor a sequence of distinct names.
        """
        if isinstance(transactions, bool):
            return (None,) if transactions else ()
        if isinstance(transactions, str) or not isinstance(transactions, Sequence):
            raise ConfigurationError(
                f"{transactions_option} must be a bool or a sequence of connection names, got {transactions!r}"
            )
        names = tuple(transactions)
        if not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
            raise ConfigurationError(f"{transactions_option} must name each connection once, got {transactions!r}")
        return names

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

        Raises:
            ConfigurationError: A transaction names a connection the configuration lacks, the
                application's check fails, or the context is open already.
        """
        if self.context is not None:
            raise ConfigurationError("The Hare context of the application is open already")
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
                self.check_application()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Closes the connections - waiting for the background observers still running -
        and the Hare context. Does nothing when it isn't open."""
        context, self.context = self.context, None
        if context is None:
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
