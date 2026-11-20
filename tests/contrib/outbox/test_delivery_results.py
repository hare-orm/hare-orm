"""Transport results retain identity across reordering, routing and partial failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hare.contrib.outbox import DeliveryResult, OutboxDelivery, OutboxRelay, TopicRouter, WebhookDelivery
from hare.contrib.outbox.brokers.redis_connection import RedisConnection
from hare.contrib.outbox.deliveries.delivery_batch import DeliveryBatch
from hare.contrib.outbox.deliveries.kafka_delivery import KafkaDelivery
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent, SubscriberOutboxEvent
from tests.utils.model_bindings import ModelBindings


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [0, -1])
async def test_nonpositive_concurrency_is_refused_before_sending(concurrency):
    send = AsyncMock()
    delivery = OutboxRelay(DemoOutboxEvent, send).delivery
    apps = ModelBindings.prepare(DemoOutboxEvent)
    event = apps.new(DemoOutboxEvent, topic="book.changed", payload={})
    with pytest.raises(ConfigurationError, match="concurrency"):
        await delivery.deliver_batch([event], concurrency=concurrency, timeout_seconds=10)
    send.assert_not_awaited()


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_router_closes_all_distinct_transports_after_a_close_failure():
    first, second = Mock(spec=OutboxDelivery), Mock(spec=OutboxDelivery)
    failure = RuntimeError("transport close failed")
    first.close = AsyncMock(side_effect=failure)
    second.close = AsyncMock()
    router = TopicRouter({"book.*": first, "author.*": second}, default=first)
    with pytest.raises(RuntimeError) as raised:
        await router.close()
    assert raised.value is failure
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_router_closes_a_shared_transport_even_when_its_buffer_is_empty():
    delivered = []
    releases = []

    class EmptyTransport(OutboxDelivery):
        def __len__(self):
            return 0

        async def deliver(self, event):
            delivered.append(event.topic)

        async def close(self):
            releases.append(self)

    transport = EmptyTransport()
    router = TopicRouter({"book.changed": transport}, default=transport)
    apps = ModelBindings.prepare(DemoOutboxEvent)
    for topic in ("book.changed", "book.deleted"):
        await router.deliver(apps.new(DemoOutboxEvent, topic=topic, payload={}))

    await router.close()

    assert delivered == ["book.changed", "book.deleted"]
    assert releases == [transport]


@pytest.mark.asyncio
async def test_reordered_transport_results_and_failing_route_are_recorded_by_event_id(db_outbox):
    events = [await DemoOutboxEvent.enqueue(topic, {}) for topic in ("book.one", "book.two", "author.one")]
    first_transport = Mock(spec=OutboxDelivery)
    first_transport.deliver_batch = AsyncMock(
        return_value=[DeliveryResult(events[1].id, RuntimeError("refused")), DeliveryResult(events[0].id)]
    )
    second_transport = Mock(spec=OutboxDelivery)
    second_transport.deliver_batch = AsyncMock(side_effect=RuntimeError("unavailable"))

    relay = OutboxRelay(DemoOutboxEvent, TopicRouter({"book.*": first_transport, "author.*": second_transport}))
    assert await relay.poll_once() == 3
    for event in events:
        await event.refresh_from_db()
    assert [event.published_at is not None for event in events] == [True, False, False]
    assert [event.last_error for event in events] == [None, "refused", "unavailable"]
    first_transport.deliver_batch.assert_awaited_once()
    second_transport.deliver_batch.assert_awaited_once()


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["missing", "duplicate", "foreign"])
async def test_malformed_batch_cannot_acknowledge_another_event(response):
    apps = ModelBindings.prepare(DemoOutboxEvent)
    events = [apps.new(DemoOutboxEvent, topic="book.changed", payload={}) for _ in range(3)]
    results = {
        "missing": [DeliveryResult(events[0].id)],
        "duplicate": [DeliveryResult(events[0].id), DeliveryResult(events[0].id)],
        "foreign": [DeliveryResult(events[0].id), DeliveryResult(events[2].id)],
    }
    delivery = Mock(spec=OutboxDelivery)
    delivery.deliver_batch = AsyncMock(return_value=results[response])
    outcomes = await DeliveryBatch.deliver(delivery, events[:2], concurrency=2, timeout_seconds=10)
    assert {result.event_id for result in outcomes} == {event.id for event in events[:2]}
    assert all(result.error is not None for result in outcomes)


@pytest.mark.database_independent
@pytest.mark.requires("http")
@pytest.mark.asyncio
async def test_webhook_returns_attempt_metadata_without_accessing_storage():
    apps = ModelBindings.prepare(SubscriberOutboxEvent)
    event = apps.new(SubscriberOutboxEvent, topic="book.changed", payload={}, subscriber_url="https://example.invalid")
    delivery = WebhookDelivery(event.subscriber_url, allows_url=lambda url: False, result_field="webhook_result")
    result = await delivery.deliver(event)
    assert result.event_id == event.id
    assert result.error is not None
    assert result.metadata["webhook_result"]["error"]
    assert event.webhook_result is None


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_kafka_retrieves_an_error_after_the_delivery_confirmation_times_out(monkeypatch):
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    class Producer:
        def __init__(self):
            self.confirmation = loop.create_future()

        async def send(self, topic, **kwargs):
            return self.confirmation

    monkeypatch.setattr(KafkaDelivery, "aiokafka", SimpleNamespace(AIOKafkaProducer=Producer))
    try:
        apps = ModelBindings.prepare(DemoOutboxEvent)
        event = apps.new(DemoOutboxEvent, topic="book.changed", payload={})
        producer = Producer()
        (result,) = await KafkaDelivery(producer=producer).deliver_batch([event], concurrency=1, timeout_seconds=0.03)
        assert result.event_id == event.id
        assert isinstance(result.error, TimeoutError)
        assert not producer.confirmation.done()
        producer.confirmation.set_exception(RuntimeError("late delivery failed"))
        await asyncio.sleep(0)
        del producer.confirmation
        assert errors == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["redis", pytest.param("webhook", marks=pytest.mark.requires("http"))])
async def test_cancellation_waits_for_outbox_client_close(broker):
    entered, finish = asyncio.Event(), asyncio.Event()
    closed = []

    async def close_client():
        entered.set()
        await finish.wait()
        closed.append(True)

    client = SimpleNamespace(aclose=close_client)
    delivery = (
        RedisConnection("redis://localhost", None) if broker == "redis" else WebhookDelivery("https://test.invalid")
    )
    delivery.client = client
    close = asyncio.create_task(delivery.close())
    await entered.wait()
    close.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    await delivery.close()

    assert closed == [True]
