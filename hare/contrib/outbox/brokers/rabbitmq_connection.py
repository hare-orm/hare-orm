from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

try:
    import aio_pika
except ImportError:
    aio_pika = None  # type: ignore[assignment]


class RabbitMQConnection:
    """One AMQP connection of a delivery or a wakeup and the exchange it uses, both opened on first
    use and closed together.

    Args:
        url: The AMQP URL.
        open_exchange: Opens the exchange on a new connection.
    """

    #: The ``aio_pika`` package, None without the ``rabbitmq`` extra.
    aio_pika: Any = aio_pika

    def __init__(self, url: str, open_exchange: Callable[[Any], Awaitable[Any]]) -> None:
        self.url = url
        self.open_exchange = open_exchange
        self.connection: Any = None
        self.exchange: Any = None
        self.connection_lock = asyncio.Lock()

    async def get_exchange(self) -> Any:
        """The exchange, opened with its connection on first use.

        Returns:
            The ``aio_pika`` exchange.
        """
        async with self.connection_lock:
            if self.exchange is None:
                connection = await self.aio_pika.connect_robust(self.url)
                try:
                    exchange = await self.open_exchange(connection)
                except BaseException:
                    # A robust connection reconnects for ever - one whose exchange wasn't opened is closed.
                    await connection.close()
                    raise
                self.connection, self.exchange = connection, exchange
            return self.exchange

    async def close(self) -> None:
        """Closes the connection - the next use opens a new one."""
        async with self.connection_lock:
            if self.connection is not None:
                connection, self.connection, self.exchange = self.connection, None, None
                await connection.close()
