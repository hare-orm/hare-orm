from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.brokers.kafka_connection import KafkaConnection
from hare.contrib.outbox.constants import OUTBOX_RELEASE_FAILED
from hare.contrib.outbox.wakeups.constants import (
    DEFAULT_WAKEUP_CHANNEL,
    KAFKA_WAKEUP_RETENTION_MILLISECONDS,
    WAKEUP_TOPIC_SEPARATOR,
)
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.contrib.package_checks import PackageChecks
from hare.core.resource_completion import ResourceCompletion

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient

try:
    import aiokafka
    import aiokafka.admin
    import aiokafka.errors
except ImportError:
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
        ConfigurationError: The ``aiokafka`` package isn't installed.
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
        PackageChecks.require_package(self.aiokafka, "KafkaWakeup", "aiokafka", "kafka")
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.create_topic = create_topic
        self.replication_factor = replication_factor
        self.broker_connection = KafkaConnection(
            partial(self.aiokafka.AIOKafkaProducer, bootstrap_servers=bootstrap_servers)
        )

    async def signal(self, topics: frozenset[str]) -> None:
        producer = await self.broker_connection.get_producer()
        await producer.send_and_wait(self.topic, WAKEUP_TOPIC_SEPARATOR.join(sorted(topics)).encode())

    async def ensure_topic(self) -> None:
        """Creates the topic of the signals unless it exists."""
        admin = self.aiokafka.admin.AIOKafkaAdminClient(bootstrap_servers=self.bootstrap_servers)
        try:
            try:
                await admin.start()
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
        except BaseException as error:
            try:
                await ResourceCompletion.wait(admin.close())
            except BaseException as release_error:
                if release_error is error:
                    raise
                raise BaseExceptionGroup(OUTBOX_RELEASE_FAILED, [error, release_error]) from None
            raise
        else:
            await ResourceCompletion.wait(admin.close())

    async def listen(self, connection: str | DatabaseClient) -> None:
        if self.create_topic:
            await self.ensure_topic()
        consumer = self.aiokafka.AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=None,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        try:
            await consumer.start()
            async for message in consumer:
                text = (message.value or b"").decode()
                self.dispatch(frozenset(topic for topic in text.split(WAKEUP_TOPIC_SEPARATOR) if topic))
        except BaseException as error:
            try:
                await ResourceCompletion.wait(consumer.stop())
            except BaseException as release_error:
                if release_error is error:
                    raise
                raise BaseExceptionGroup(OUTBOX_RELEASE_FAILED, [error, release_error]) from None
            raise
        else:
            await ResourceCompletion.wait(consumer.stop())
        raise ConnectionError("the Kafka consumer stopped")

    async def close(self) -> None:
        await self.broker_connection.close()
