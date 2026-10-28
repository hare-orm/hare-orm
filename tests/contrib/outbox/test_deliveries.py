"""Deliveries that need no broker: TopicRouter routes by topic and pattern, fails a topic without a
route; WebhookDelivery posts the payload, signed, to an HTTP server of the test; KafkaDelivery fails an
event its producer refuses alone; the relay batches through them."""

import asyncio
import hashlib
import hmac
import json

import pytest

from hare.contrib.outbox import DeliveryError, OutboxRelay, TopicRouter, WebhookDelivery
from hare.contrib.outbox.deliveries.constants import EVENT_ID_HEADER, EVENT_TOPIC_HEADER, WEBHOOK_SIGNATURE_HEADER
from tests.contrib.outbox.http_receiver import HttpReceiver
from tests.contrib.outbox.models import DemoOutboxEvent


@pytest.mark.asyncio
async def test_topic_router_routes_by_topic_then_pattern_then_default(db_outbox):
    routed: list[tuple[str, str]] = []

    def route(name: str):
        async def deliver(event: DemoOutboxEvent) -> None:
            routed.append((name, event.topic))

        return deliver

    router = TopicRouter(
        {"shop.order.paid": route("exact"), "shop.order.*": route("pattern")}, default=route("default")
    )
    for topic in ("shop.order.paid", "shop.order.created", "billing.charged"):
        await DemoOutboxEvent.enqueue(topic, {})

    assert await OutboxRelay(DemoOutboxEvent, router, batch_size=10).poll_once() == 3
    assert sorted(routed) == [
        ("default", "billing.charged"),
        ("exact", "shop.order.paid"),
        ("pattern", "shop.order.created"),
    ]


class RefusingKafkaProducer:
    """Answers ``send()`` as an ``AIOKafkaProducer`` does - a confirmation future, or at once an
    error for a message it refuses (too large); never confirms a message marked to hang."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, topic: str, value: bytes, key: bytes | None = None, headers: object = None) -> object:
        if b"too-large" in value:
            raise ValueError("MessageSizeTooLargeError")
        self.sent.append(value)
        confirmation = asyncio.get_running_loop().create_future()
        if b"unconfirmed" not in value:
            confirmation.set_result(None)
        return confirmation


@pytest.mark.requires("kafka")
@pytest.mark.asyncio
async def test_a_kafka_event_the_producer_refuses_fails_alone(db_outbox):
    from hare.contrib.outbox import KafkaDelivery

    producer = RefusingKafkaProducer()
    for number, mark in enumerate(("ok", "ok", "too-large", "ok")):
        await DemoOutboxEvent.enqueue("widget.created", {"number": number, "mark": mark}, ordering_key=str(number))
    relay = OutboxRelay(
        DemoOutboxEvent, KafkaDelivery(producer=producer), batch_size=10, retry_base_seconds=0, retry_max_seconds=0
    )
    assert await relay.poll_once() == 4
    events = await DemoOutboxEvent.objects.order_by("sequence")
    assert [event.published_at is not None for event in events] == [True, True, False, True]
    # Each event the producer took is sent once.
    assert len(producer.sent) == 3

    events = [DemoOutboxEvent(topic="widget.created", payload={"mark": mark}) for mark in ("ok", "unconfirmed")]
    outcomes = await KafkaDelivery(producer=producer).deliver_batch(events, concurrency=1, timeout_seconds=0.05)
    assert outcomes[0] is None
    assert isinstance(outcomes[1], TimeoutError)
    assert str(outcomes[1]) == "the batch wasn't confirmed within 0.05 seconds"


@pytest.mark.asyncio
async def test_a_topic_without_a_route_fails(db_outbox):
    async def deliver(event: DemoOutboxEvent) -> None:
        pass

    event = await DemoOutboxEvent.enqueue("billing.charged", {})
    await OutboxRelay(DemoOutboxEvent, TopicRouter({"shop.*": deliver})).poll_once()

    await event.refresh_from_db()
    assert event.published_at is None
    assert "no delivery takes topic 'billing.charged'" in event.last_error
    with pytest.raises(DeliveryError):
        TopicRouter({}).get_delivery("anything")


@pytest.mark.requires("http")
@pytest.mark.asyncio
async def test_a_webhook_posts_the_payload_signed(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, headers={"author": "alice", "attempt": 2})
    receiver = HttpReceiver()
    async with receiver as url:
        delivery = WebhookDelivery(url, secret="s3cret", headers={"x-service": "shop"})
        relay = OutboxRelay(DemoOutboxEvent, delivery)
        assert await relay.poll_once() == 1
        await delivery.close()

    ((headers, body),) = receiver.requests
    assert json.loads(body) == {"n": 1}
    assert headers[EVENT_ID_HEADER] == str(event.id)
    assert headers[EVENT_TOPIC_HEADER] == "widget.created"
    assert headers["author"] == "alice"
    assert headers["attempt"] == "2"
    assert headers["x-service"] == "shop"
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers[WEBHOOK_SIGNATURE_HEADER] == expected
    await event.refresh_from_db()
    assert event.published_at is not None


@pytest.mark.requires("http")
@pytest.mark.asyncio
async def test_a_webhook_sends_a_header_value_beyond_ascii(db_outbox):
    """The client wrote a text header only in ASCII - every delivery of the event failed."""
    event = await DemoOutboxEvent.enqueue("widget.created", {}, headers={"author": "Zoë"})
    receiver = HttpReceiver()
    async with receiver as url:
        delivery = WebhookDelivery(url, headers={"x-service": "магазин"})
        assert await OutboxRelay(DemoOutboxEvent, delivery).poll_once() == 1
        await delivery.close()

    ((headers, __),) = receiver.requests
    assert headers["author"] == "Zoë"
    assert headers["x-service"] == "магазин"
    await event.refresh_from_db()
    assert event.published_at is not None


@pytest.mark.requires("http")
@pytest.mark.asyncio
async def test_a_webhook_answering_an_error_fails_the_event(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.created", {})
    async with HttpReceiver(status=503) as url:
        delivery = WebhookDelivery(url)
        await OutboxRelay(DemoOutboxEvent, delivery).poll_once()
        await delivery.close()

    await event.refresh_from_db()
    assert event.published_at is None
    assert "503" in event.last_error


@pytest.mark.requires("http")
@pytest.mark.asyncio
async def test_a_webhook_event_with_a_header_name_beyond_ascii_fails_with_a_reason(db_outbox):
    """The client's own encoding error failed the event's every delivery."""
    event = await DemoOutboxEvent.enqueue("widget.created", {}, headers={"x-имя": "v"})
    async with HttpReceiver() as url:
        delivery = WebhookDelivery(url)
        await OutboxRelay(DemoOutboxEvent, delivery).poll_once()
        await delivery.close()

    await event.refresh_from_db()
    assert event.published_at is None
    assert "header name is ASCII" in event.last_error
