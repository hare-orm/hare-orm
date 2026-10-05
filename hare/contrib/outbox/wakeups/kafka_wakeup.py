from __future__ import annotations

import asyncio
from typing import Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.wakeups.constants import (
    DEFAULT_WAKEUP_CHANNEL,
    KAFKA_WAKEUP_RETENTION_MILLISECONDS,
    WAKEUP_TOPIC_SEPARATOR,
)
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.exceptions import ConfigurationError

try:
    import aiokafka
    import aiokafka.admin
    import aiokafka.errors
except ImportError:  # pragma: nocoverage
    aiokafka = None


class KafkaWakeup(OutboxWakeup):
    """Wakes the relays through a Kafka topic of signals - each relay reads every partition from the
    end, outside any consumer group, and the topic keeps a signal only briefly. Needs the ``kafka``
    extra.

    Args:
        bootstrap_servers: The Kafka servers (``"localhost:9092"``).
        topic: The topic of the signals.
        create_topic: Create the topic, keeping signals for a minute, when the relays start - if it
            doesn't exist.
        replication_factor: The replication of a created topic.

    Raises:
        ConfigurationError: An empty ``bootstrap_servers`` or ``topic``, a ``replication_factor``
            below 1, or the ``aiokafka`` package isn't installed.
    """

    #: The ``aiokafka`` package, None without the ``kafka`` extra.
    aiokafka: Any = aiokafka

    def __init__(
        self,
        bootstrap_servers: str | list[str],
        *,
        topic: str = DEFAULT_WAKEUP_CHANNEL,
        create_topic: bool = True,
        replication_factor: int = 1,
    ) -> None:
        super().__init__()
        BrokerArguments.require_package(self.aiokafka, "KafkaWakeup", "aiokafka", "kafka")
        if not bootstrap_servers:
            raise ConfigurationError("bootstrap_servers must name a Kafka server")
        BrokerArguments.require_text("topic", topic)
        if isinstance(replication_factor, bool) or not isinstance(replication_factor, int) or replication_factor < 1:
            raise ConfigurationError(f"replication_factor must be an int of at least 1, got {replication_factor!r}")
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.create_topic = create_topic
        self.replication_factor = replication_factor
        self.producer: Any = None
        self.producer_lock = asyncio.Lock()

    async def get_producer(self) -> Any:
        """The producer of the signals, started on first use.

        Returns:
            The ``AIOKafkaProducer``.
        """
        async with self.producer_lock:
            if self.producer is None:
                producer = self.aiokafka.AIOKafkaProducer(bootstrap_servers=self.bootstrap_servers)
                await producer.start()
                self.producer = producer
            return self.producer

    async def signal(self, topics: frozenset[str]) -> None:
        producer = await self.get_producer()
        await producer.send_and_wait(self.topic, WAKEUP_TOPIC_SEPARATOR.join(sorted(topics)).encode())

    async def ensure_topic(self) -> None:
        """Creates the topic of the signals unless it exists."""
        admin = self.aiokafka.admin.AIOKafkaAdminClient(bootstrap_servers=self.bootstrap_servers)
        await admin.start()
        try:
            await admin.create_topics(
                [
                    self.aiokafka.admin.NewTopic(
                        name=self.topic,
                        num_partitions=1,
                        replication_factor=self.replication_factor,
                        topic_configs={"retention.ms": str(KAFKA_WAKEUP_RETENTION_MILLISECONDS)},
                    )
                ]
            )
        except self.aiokafka.errors.TopicAlreadyExistsError:
            pass
        finally:
            await admin.close()

    async def listen(self, connection_alias: str) -> None:
        if self.create_topic:
            await self.ensure_topic()
        consumer = self.aiokafka.AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=None,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            async for message in consumer:
                text = (message.value or b"").decode()
                self.dispatch(frozenset(topic for topic in text.split(WAKEUP_TOPIC_SEPARATOR) if topic))
        finally:
            await consumer.stop()
        raise ConnectionError("the Kafka consumer stopped")

    async def close(self) -> None:
        async with self.producer_lock:
            if self.producer is not None:
                producer, self.producer = self.producer, None
                await producer.stop()
