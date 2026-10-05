"""The wakeups and deliveries of a broker, against a live one - each test skipped without its
broker's variable: HARE_TEST_REDIS_URL, HARE_TEST_KAFKA_BOOTSTRAP, HARE_TEST_RABBITMQ_URL
(``make test_brokers_up`` starts all three)."""

import asyncio
import json
import os
import uuid

import pytest

from hare.contrib.outbox import (
    KafkaDelivery,
    KafkaWakeup,
    OutboxRelay,
    RabbitMQDelivery,
    RabbitMQWakeup,
    RedisStreamsDelivery,
    RedisWakeup,
)
from hare.contrib.outbox.deliveries.constants import EVENT_ID_HEADER, REDIS_STREAM_EVENT_FIELD
from tests.contrib.outbox.models import DemoOutboxEvent

REDIS_URL = os.environ.get("HARE_TEST_REDIS_URL")
KAFKA_BOOTSTRAP = os.environ.get("HARE_TEST_KAFKA_BOOTSTRAP")
RABBITMQ_URL = os.environ.get("HARE_TEST_RABBITMQ_URL")

needs_redis = pytest.mark.skipif(not REDIS_URL, reason="HARE_TEST_REDIS_URL isn't set")
needs_kafka = pytest.mark.skipif(not KAFKA_BOOTSTRAP, reason="HARE_TEST_KAFKA_BOOTSTRAP isn't set")
needs_rabbitmq = pytest.mark.skipif(not RABBITMQ_URL, reason="HARE_TEST_RABBITMQ_URL isn't set")


def get_unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def assert_wakes_the_relay(wakeup) -> None:
    """A relay polling once a minute delivers an event within seconds - woken by ``wakeup``."""
    delivered = asyncio.Event()

    async def deliver(_event: DemoOutboxEvent) -> None:
        delivered.set()

    async with OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=60, wakeup=wakeup):
        await asyncio.sleep(1.5)  # the subscription connects
        await DemoOutboxEvent.enqueue("widget.created", {}, wakeup=wakeup)
        await asyncio.wait_for(delivered.wait(), timeout=15)
    await wakeup.close()


@needs_redis
@pytest.mark.asyncio
async def test_redis_wakeup(db_outbox):
    await assert_wakes_the_relay(RedisWakeup(REDIS_URL, channel=get_unique_name("hare-wakeup")))


@needs_redis
@pytest.mark.asyncio
async def test_redis_streams_delivery(db_outbox):
    stream = get_unique_name("hare-stream")
    first = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, ordering_key="widget:1")
    await DemoOutboxEvent.enqueue("widget.created", {"n": 2})
    delivery = RedisStreamsDelivery(REDIS_URL, stream=stream, max_length=100)
    assert await OutboxRelay(DemoOutboxEvent, delivery, batch_size=10).poll_once() == 2

    entries = await delivery.get_client().xrange(stream)
    await delivery.get_client().delete(stream)
    await delivery.close()
    assert len(entries) == 2
    first_fields = entries[0][1]
    assert first_fields[EVENT_ID_HEADER.encode()].decode() == str(first.id)
    document = json.loads(first_fields[REDIS_STREAM_EVENT_FIELD.encode()])
    assert document["payload"] == {"n": 1}
    assert document["ordering_key"] == "widget:1"


@needs_kafka
@pytest.mark.asyncio
async def test_kafka_wakeup(db_outbox):
    await assert_wakes_the_relay(KafkaWakeup(KAFKA_BOOTSTRAP, topic=get_unique_name("hare-wakeup")))


@needs_kafka
@pytest.mark.asyncio
async def test_kafka_delivery(db_outbox):
    import aiokafka

    topic = get_unique_name("hare-events")
    event = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, ordering_key="widget:1", headers={"a": "b"})
    delivery = KafkaDelivery(KAFKA_BOOTSTRAP, topic=topic)
    assert await OutboxRelay(DemoOutboxEvent, delivery).poll_once() == 1
    await delivery.close()

    consumer = aiokafka.AIOKafkaConsumer(topic, bootstrap_servers=KAFKA_BOOTSTRAP, auto_offset_reset="earliest")
    await consumer.start()
    try:
        message = await asyncio.wait_for(consumer.getone(), timeout=15)
    finally:
        await consumer.stop()
    assert json.loads(message.value) == {"n": 1}
    assert message.key == b"widget:1"
    headers = dict(message.headers)
    assert headers[EVENT_ID_HEADER] == str(event.id).encode()
    assert headers["a"] == b"b"
    await event.refresh_from_db()
    assert event.published_at is not None


@needs_rabbitmq
@pytest.mark.asyncio
async def test_rabbitmq_wakeup(db_outbox):
    await assert_wakes_the_relay(RabbitMQWakeup(RABBITMQ_URL, exchange=get_unique_name("hare-wakeup")))


@needs_rabbitmq
@pytest.mark.asyncio
async def test_rabbitmq_delivery(db_outbox):
    import aio_pika

    queue_name = get_unique_name("hare-events")
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        channel = await connection.channel()
        # Durable - RabbitMQ 4 refuses transient queues other than exclusive ones.
        queue = await channel.declare_queue(queue_name, durable=True)
        event = await DemoOutboxEvent.enqueue("widget.created", {"n": 1})
        delivery = RabbitMQDelivery(RABBITMQ_URL, routing_key=queue_name)
        assert await OutboxRelay(DemoOutboxEvent, delivery).poll_once() == 1
        await delivery.close()
        message = await queue.get(timeout=15)
        await message.ack()
        await queue.delete()
    finally:
        await connection.close()
    assert json.loads(message.body) == {"n": 1}
    assert message.message_id == str(event.id)
    assert message.headers[EVENT_ID_HEADER] == str(event.id)
