"""The Hare context of a web application's lifetime - the same for every framework."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.base import RequestQuery
from hare.core.context import HareContext
from hare.core.hare import Hare
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.config import HareConfig


class HareLifecycle:
    """Binds an application's models, opens its Hare context when it starts and closes it when it
    stops.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        atomic_requests: True for a transaction per request on the default connection, the
            connection names for one on each of them, False for none.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor a sequence of names.
    """

    def __init__(self, config: dict[str, Any] | HareConfig, *, atomic_requests: bool | Sequence[str] = False) -> None:
        self.config = config
        self.transaction_connection_names = self.get_transaction_connection_names(atomic_requests)
        self.context: HareContext | None = None

    @staticmethod
    def get_transaction_connection_names(atomic_requests: bool | Sequence[str]) -> tuple[str | None, ...]:
        """The connections a request's transaction runs on.

        Args:
            atomic_requests: True, False, or connection names.

        Returns:
            ``(None,)`` - the default connection - for True, the names for a sequence, nothing for
            False.

        Raises:
            ConfigurationError: See the class.
        """
        if isinstance(atomic_requests, bool):
            return (None,) if atomic_requests else ()
        if isinstance(atomic_requests, str) or not isinstance(atomic_requests, Sequence):
            raise ConfigurationError(
                f"atomic_requests must be a bool or a sequence of connection names, got {atomic_requests!r}"
            )
        names = tuple(atomic_requests)
        if not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
            raise ConfigurationError(f"atomic_requests must name each connection once, got {atomic_requests!r}")
        return names

    def bind_models(self) -> None:
        """Binds the configuration's models without connections - a framework building its
        handlers' signatures before the application starts reads the request queries' parameters
        from them (``Meta.filters``, descriptions).

        Raises:
            ConfigurationError: The configuration is wrong.
        """
        Hare.bind_models(self.config)

    async def start(self) -> None:
        """Opens the Hare context as the global fallback - not as the calling task's own context,
        so the requests a framework runs in tasks of their own see it and another task may close
        it - and checks the request transactions' connections and every request query.

        Raises:
            ConfigurationError: A request transaction names a connection the configuration lacks,
                a request query is wrong, or the context is open already.
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
                for connection_name in self.transaction_connection_names:
                    if connection_name is not None and connection_name not in aliases:
                        raise ConfigurationError(
                            f"atomic_requests names the connection {connection_name!r}, configured are: {aliases}"
                        )
                RequestQuery.check_declarations()
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
