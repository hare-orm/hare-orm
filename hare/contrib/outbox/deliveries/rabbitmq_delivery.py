from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.outbox.outbox_event import OutboxEvent

try:
    import aio_pika
except ImportError:  # pragma: nocoverage
    aio_pika = None  # type: ignore[assignment]


class RabbitMQDelivery(OutboxDelivery):
    """Sends events to RabbitMQ - persistent messages through ``exchange`` with the event's topic as
    the routing key (or ``routing_key``), each confirmed by the broker before the event counts as
    delivered (publisher confirms). Needs the ``rabbitmq`` extra.

    Args:
        url: The AMQP URL (``amqp://guest:guest@localhost/``).
        exchange: An existing exchange; the default exchange when empty - the routing key then names
            a queue.
        routing_key: The routing key of every event; None for each event's own topic.

    Raises:
        ConfigurationError: An empty ``url`` or ``routing_key``, or the ``aio-pika`` package isn't
            installed.
    """

    #: The ``aio_pika`` package, None without the ``rabbitmq`` extra.
    aio_pika: Any = aio_pika

    def __init__(self, url: str, *, exchange: str = "", routing_key: str | None = None) -> None:
        BrokerArguments.require_package(self.aio_pika, "RabbitMQDelivery", "aio-pika", "rabbitmq")
        BrokerArguments.require_text("url", url, "AMQP URL")
        if not isinstance(exchange, str):
            raise ConfigurationError(f"exchange must be a string, got {exchange!r}")
        if routing_key is not None and (not isinstance(routing_key, str) or not routing_key):
            raise ConfigurationError(f"routing_key must be None or a non-empty string, got {routing_key!r}")
        self.url = url
        self.exchange_name = exchange
        self.routing_key = routing_key
        self.connection: Any = None
        self.exchange: Any = None
        self.connection_lock = asyncio.Lock()

    async def get_exchange(self) -> Any:
        """The exchange, on a channel with publisher confirms, opened on first use.

        Returns:
            The ``aio_pika`` exchange.
        """
        async with self.connection_lock:
            if self.exchange is None:
                self.connection = await self.aio_pika.connect_robust(self.url)
                channel = await self.connection.channel(publisher_confirms=True)
                self.exchange = (
                    await channel.get_exchange(self.exchange_name) if self.exchange_name else channel.default_exchange
                )
            return self.exchange

    async def deliver(self, event: OutboxEvent) -> None:
        exchange = await self.get_exchange()
        message = self.aio_pika.Message(
            body=self.get_body(event),
            headers=self.get_headers(event),
            message_id=str(event.id),
            content_type="application/json",
            delivery_mode=self.aio_pika.DeliveryMode.PERSISTENT,
        )
        await exchange.publish(message, routing_key=self.routing_key or event.topic)

    async def close(self) -> None:
        async with self.connection_lock:
            if self.connection is not None:
                connection, self.connection, self.exchange = self.connection, None, None
                await connection.close()
