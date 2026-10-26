from __future__ import annotations

from typing import Any

from hare.contrib.outbox.brokers.rabbitmq_connection import RabbitMQConnection
from hare.contrib.outbox.wakeups.constants import DEFAULT_WAKEUP_CHANNEL, WAKEUP_TOPIC_SEPARATOR
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.contrib.package_checks import PackageChecks


class RabbitMQWakeup(OutboxWakeup):
    """Wakes the relays through a RabbitMQ fanout exchange - each relay reads the signals through an
    exclusive queue of its own, removed with it. Needs the ``rabbitmq`` extra.

    Args:
        url: The AMQP URL (``amqp://guest:guest@localhost/``).
        exchange: The fanout exchange of the signals.

    Raises:
        ConfigurationError: The ``aio-pika`` package isn't installed.
    """

    def __init__(self, url: str, *, exchange: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        PackageChecks.require_package(RabbitMQConnection.aio_pika, "RabbitMQWakeup", "aio-pika", "rabbitmq")
        self.exchange_name = exchange
        self.broker_connection = RabbitMQConnection(url, self.open_exchange)

    async def open_exchange(self, connection: Any) -> Any:
        """Declares the fanout exchange of the signals.

        Args:
            connection: The new ``aio_pika`` connection.

        Returns:
            The ``aio_pika`` exchange.
        """
        channel = await connection.channel()
        return await channel.declare_exchange(
            self.exchange_name, RabbitMQConnection.aio_pika.ExchangeType.FANOUT, durable=True
        )

    async def signal(self, topics: frozenset[str]) -> None:
        exchange = await self.broker_connection.get_exchange()
        body = WAKEUP_TOPIC_SEPARATOR.join(sorted(topics)).encode()
        await exchange.publish(RabbitMQConnection.aio_pika.Message(body=body), routing_key="")

    async def listen(self, connection_alias: str) -> None:
        exchange = await self.broker_connection.get_exchange()
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
        await self.broker_connection.close()
