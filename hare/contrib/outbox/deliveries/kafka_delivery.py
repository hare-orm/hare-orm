from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.outbox.outbox_event import OutboxEvent

try:
    import aiokafka
except ImportError:  # pragma: nocoverage
    aiokafka = None


class KafkaDelivery(OutboxDelivery):
    """Sends events to Kafka - to ``topic``, or to the Kafka topic named like the event's own; keyed
    by the ordering key, so one key's events stay in one partition, in order. A batch is sent at once
    and confirmed together. Needs the ``kafka`` extra.

    Args:
        bootstrap_servers: The Kafka servers (``"localhost:9092"``) - or give ``producer``.
        producer: An ``AIOKafkaProducer`` of your own, started and left open.
        topic: The Kafka topic of every event; None for each event's own topic.

    Raises:
        ConfigurationError: Neither or both of ``bootstrap_servers`` and ``producer``, an empty
            ``topic``, or the ``aiokafka`` package isn't installed.
    """

    #: The ``aiokafka`` package, None without the ``kafka`` extra.
    aiokafka: Any = aiokafka

    def __init__(
        self, bootstrap_servers: str | list[str] | None = None, *, producer: Any = None, topic: str | None = None
    ) -> None:
        BrokerArguments.require_package(self.aiokafka, "KafkaDelivery", "aiokafka", "kafka")
        if (bootstrap_servers is None) == (producer is None):
            raise ConfigurationError("KafkaDelivery takes either bootstrap_servers or producer")
        if topic is not None and (not isinstance(topic, str) or not topic):
            raise ConfigurationError(f"topic must be None or a non-empty string, got {topic!r}")
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.owns_producer = producer is None
        self.producer: Any = producer
        self.producer_lock = asyncio.Lock()

    async def get_producer(self) -> Any:
        """The producer, started on first use.

        Returns:
            The ``AIOKafkaProducer``.
        """
        async with self.producer_lock:
            if self.producer is None:
                producer = self.aiokafka.AIOKafkaProducer(bootstrap_servers=self.bootstrap_servers, acks="all")
                await producer.start()
                self.producer = producer
            return self.producer

    async def send(self, producer: Any, event: OutboxEvent) -> Any:
        """Hands an event to the producer.

        Args:
            producer: The producer.
            event: The event.

        Returns:
            The future of its confirmation.
        """
        return await producer.send(
            self.topic or event.topic,
            value=self.get_body(event),
            key=self.get_key(event),
            headers=[(name, value.encode()) for name, value in self.get_headers(event).items()],
        )

    async def deliver(self, event: OutboxEvent) -> None:
        await (await self.send(await self.get_producer(), event))

    async def deliver_batch(
        self, events: Sequence[OutboxEvent], *, concurrency: int, timeout_seconds: float
    ) -> list[BaseException | None]:
        producer = await self.get_producer()

        async def send_all() -> list[Any]:
            confirmations = [await self.send(producer, event) for event in events]
            return await asyncio.gather(*confirmations, return_exceptions=True)

        try:
            results = await asyncio.wait_for(send_all(), timeout_seconds)
        except Exception as error:
            return [error] * len(events)
        return [result if isinstance(result, BaseException) else None for result in results]

    async def close(self) -> None:
        async with self.producer_lock:
            if self.owns_producer and self.producer is not None:
                producer, self.producer = self.producer, None
                await producer.stop()
