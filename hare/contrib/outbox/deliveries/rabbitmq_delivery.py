from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.brokers.rabbitmq_connection import RabbitMQConnection
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent


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

    def __init__(self, url: str, *, exchange: str = "", routing_key: str | None = None) -> None:
        BrokerArguments.require_package(RabbitMQConnection.aio_pika, "RabbitMQDelivery", "aio-pika", "rabbitmq")
        BrokerArguments.require_text("url", url, "AMQP URL")
        if routing_key is not None and (not isinstance(routing_key, str) or not routing_key):
            raise ConfigurationError(f"routing_key must be None or a non-empty string, got {routing_key!r}")
        self.exchange_name = exchange
        self.routing_key = routing_key
        self.broker_connection = RabbitMQConnection(url, self.open_exchange)

    async def open_exchange(self, connection: Any) -> Any:
        """Opens the exchange on a channel with publisher confirms.

        Args:
            connection: The new ``aio_pika`` connection.

        Returns:
            The ``aio_pika`` exchange.
        """
        channel = await connection.channel(publisher_confirms=True)
        return await channel.get_exchange(self.exchange_name) if self.exchange_name else channel.default_exchange

    async def deliver(self, event: OutboxEvent) -> None:
        exchange = await self.broker_connection.get_exchange()
        message = RabbitMQConnection.aio_pika.Message(
            body=self.get_body(event),
            headers=self.get_headers(event),
            message_id=str(event.id),
            content_type="application/json",
            delivery_mode=RabbitMQConnection.aio_pika.DeliveryMode.PERSISTENT,
        )
        await exchange.publish(message, routing_key=self.routing_key or event.topic)

    async def close(self) -> None:
        await self.broker_connection.close()
