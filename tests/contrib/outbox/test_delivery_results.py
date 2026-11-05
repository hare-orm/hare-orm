"""Transport results retain identity across reordering, routing and partial failures."""

from unittest.mock import AsyncMock, Mock

import pytest

from hare.contrib.outbox import DeliveryResult, OutboxDelivery, OutboxRelay, TopicRouter, WebhookDelivery
from hare.contrib.outbox.deliveries.delivery_batch import DeliveryBatch
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent, SubscriberOutboxEvent


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [0, -1])
async def test_nonpositive_concurrency_is_refused_before_sending(concurrency):
    send = AsyncMock()
    delivery = OutboxRelay(DemoOutboxEvent, send).delivery
    event = DemoOutboxEvent(topic="book.changed", payload={})
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
    events = [DemoOutboxEvent(topic="book.changed", payload={}) for _ in range(3)]
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
    event = SubscriberOutboxEvent(topic="book.changed", payload={}, subscriber_url="https://example.invalid")
    delivery = WebhookDelivery(event.subscriber_url, allows_url=lambda url: False, result_field="webhook_result")
    result = await delivery.deliver(event)
    assert result.event_id == event.id
    assert result.error is not None
    assert result.metadata["webhook_result"]["error"]
    assert event.webhook_result is None
