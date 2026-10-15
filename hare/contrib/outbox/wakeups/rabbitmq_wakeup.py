from __future__ import annotations

import asyncio
from typing import Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.wakeups.constants import DEFAULT_WAKEUP_CHANNEL, WAKEUP_TOPIC_SEPARATOR
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup

try:
    import aio_pika
except ImportError:  # pragma: nocoverage
    aio_pika = None  # type: ignore[assignment]


class RabbitMQWakeup(OutboxWakeup):
    """Wakes the relays through a RabbitMQ fanout exchange - each relay reads the signals through an
    exclusive queue of its own, removed with it. Needs the ``rabbitmq`` extra.

    Args:
        url: The AMQP URL (``amqp://guest:guest@localhost/``).
        exchange: The fanout exchange of the signals.

    Raises:
        ConfigurationError: An empty ``url`` or ``exchange``, or the ``aio-pika`` package isn't
            installed.
    """

    #: The ``aio_pika`` package, None without the ``rabbitmq`` extra.
    aio_pika: Any = aio_pika

    def __init__(self, url: str, *, exchange: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        BrokerArguments.require_package(self.aio_pika, "RabbitMQWakeup", "aio-pika", "rabbitmq")
        BrokerArguments.require_text("url", url, "AMQP URL")
        BrokerArguments.require_text("exchange", exchange)
        self.url = url
        self.exchange_name = exchange
        self.connection: Any = None
        self.exchange: Any = None
        self.connection_lock = asyncio.Lock()

    async def get_exchange(self) -> Any:
        """The exchange of the signals, declared on first use.

        Returns:
            The ``aio_pika`` exchange.
        """
        async with self.connection_lock:
            if self.exchange is None:
                self.connection = await self.aio_pika.connect_robust(self.url)
                channel = await self.connection.channel()
                self.exchange = await channel.declare_exchange(
                    self.exchange_name, self.aio_pika.ExchangeType.FANOUT, durable=True
                )
            return self.exchange

    async def signal(self, topics: frozenset[str]) -> None:
        exchange = await self.get_exchange()
        body = WAKEUP_TOPIC_SEPARATOR.join(sorted(topics)).encode()
        await exchange.publish(self.aio_pika.Message(body=body), routing_key="")

    async def listen(self, connection_alias: str) -> None:
        exchange = await self.get_exchange()
        channel = exchange.channel
        queue = await channel.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange)
        async with queue.iterator() as messages:
            async for message in messages:
                async with message.process():
                    text = message.body.decode()
                    self.dispatch(frozenset(topic for topic in text.split(WAKEUP_TOPIC_SEPARATOR) if topic))
        raise ConnectionError("the RabbitMQ queue was closed")

    async def close(self) -> None:
        async with self.connection_lock:
            if self.connection is not None:
                connection, self.connection, self.exchange = self.connection, None, None
                await connection.close()
