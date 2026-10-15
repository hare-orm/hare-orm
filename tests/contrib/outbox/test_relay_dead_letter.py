"""Failed deliveries: attempts and last_error, the pause before a retry, dead letters - no longer
delivered, holding back their ordering key until retried - their retry and cleanup, and the
observers' events."""

from datetime import timedelta

import pytest

from hare.contrib.outbox import OutboxDeadLettered, OutboxDelivered, OutboxDeliveryFailed, OutboxRelay
from hare.contrib.test import requires_features
from hare.instrumentation.observers.observers import Observers
from hare.time import Timezone
from tests.contrib.outbox.models import DemoOutboxEvent

MAX_DELIVERY_ATTEMPTS = 3


class BrokenDeliveryError(Exception):
    pass


async def always_fails(event: DemoOutboxEvent) -> None:
    raise BrokenDeliveryError("delivery backend unreachable")


async def make_due(event: DemoOutboxEvent) -> None:
    await DemoOutboxEvent.objects.filter(id=event.id).update(next_attempt_at=None)


@pytest.mark.asyncio
async def test_a_failure_is_counted_and_retried_after_a_pause(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.updated", {})
    relay = OutboxRelay(DemoOutboxEvent, always_fails, retry_base_seconds=60, retry_max_seconds=60)

    assert await relay.poll_once() == 1
    await event.refresh_from_db()
    assert event.attempts == 1
    assert "delivery backend unreachable" in event.last_error
    assert event.published_at is None
    assert event.lease_until is None
    assert event.leased_by is None
    # The pause - 60 seconds, spread by up to 10%.
    delay = (event.next_attempt_at - Timezone.now()).total_seconds()
    assert 50 < delay < 67
    # Not due yet - not claimed.
    assert await relay.poll_once() == 0


@pytest.mark.asyncio
async def test_the_pause_doubles_up_to_its_maximum(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, always_fails, retry_base_seconds=10, retry_max_seconds=25)
    delays = [relay.get_retry_delay_seconds(attempts) for attempts in (1, 2, 3, 4)]
    assert 9 <= delays[0] <= 11
    assert 18 <= delays[1] <= 22
    assert 22.5 <= delays[2] <= 27.5
    assert 22.5 <= delays[3] <= 27.5


@pytest.mark.asyncio
async def test_out_of_attempts_an_event_is_dead_lettered(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.updated", {})
    relay = OutboxRelay(DemoOutboxEvent, always_fails, max_delivery_attempts=MAX_DELIVERY_ATTEMPTS)
    dead_letters: list[OutboxDeadLettered] = []
    failures: list[OutboxDeliveryFailed] = []

    with (
        Observers.observing(OutboxDeadLettered, dead_letters.append),
        Observers.observing(OutboxDeliveryFailed, failures.append),
    ):
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            assert await relay.poll_once() == 1
            await make_due(event)
        assert await relay.poll_once() == 0

    await event.refresh_from_db()
    assert event.attempts == MAX_DELIVERY_ATTEMPTS
    assert event.dead_lettered_at is not None
    assert event.published_at is None
    assert [failure.attempts for failure in failures] == [1, 2]
    assert len(dead_letters) == 1
    assert dead_letters[0].event_id == event.id
    assert "unreachable" in dead_letters[0].error
    backlog = await relay.get_backlog()
    assert (backlog.pending, backlog.dead_lettered) == (0, 1)


@pytest.mark.asyncio
async def test_a_dead_letter_holds_back_its_ordering_key_until_retried(db_outbox):
    first = await DemoOutboxEvent.enqueue("widget.updated", {"n": 1}, ordering_key="widget:1")
    second = await DemoOutboxEvent.enqueue("widget.updated", {"n": 2}, ordering_key="widget:1")
    other = await DemoOutboxEvent.enqueue("widget.updated", {"n": 3}, ordering_key="widget:2")
    await DemoOutboxEvent.objects.filter(id=first.id).update(dead_lettered_at=Timezone.now(), attempts=5)
    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.payload["n"])

    relay = OutboxRelay(DemoOutboxEvent, deliver)
    assert await relay.poll_once() == 1
    assert delivered == [3]

    assert await relay.retry_dead_lettered(topics=["widget.updated"]) == 1
    assert await relay.poll_once() == 1
    assert await relay.poll_once() == 1
    assert delivered == [3, 1, 2]
    for event in (first, second, other):
        await event.refresh_from_db()
        assert event.published_at is not None


@pytest.mark.asyncio
async def test_retry_and_cleanup_of_dead_letters(db_outbox):
    old = await DemoOutboxEvent.enqueue("widget.updated", {})
    recent = await DemoOutboxEvent.enqueue("widget.updated", {})
    await DemoOutboxEvent.objects.filter(id=old.id).update(dead_lettered_at=Timezone.now() - timedelta(days=40))
    await DemoOutboxEvent.objects.filter(id=recent.id).update(dead_lettered_at=Timezone.now())
    relay = OutboxRelay(DemoOutboxEvent, always_fails)

    assert await relay.cleanup_dead_lettered(timedelta(days=30), batch_size=1) == 1
    assert await relay.retry_dead_lettered(ids=[recent.id]) == 1
    await recent.refresh_from_db()
    assert (recent.dead_lettered_at, recent.attempts) == (None, 0)
    assert not await DemoOutboxEvent.objects.filter(id=old.id).exists()


@pytest.mark.asyncio
async def test_a_later_success_publishes_a_failed_event(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.updated", {})
    attempt_count = 0

    async def fails_once(_event: DemoOutboxEvent) -> None:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise BrokenDeliveryError("transient")

    delivered: list[OutboxDelivered] = []
    relay = OutboxRelay(DemoOutboxEvent, fails_once)
    with Observers.observing(OutboxDelivered, delivered.append):
        await relay.poll_once()
        await make_due(event)
        await relay.poll_once()

    await event.refresh_from_db()
    assert event.attempts == 1
    assert event.published_at is not None
    assert [(item.event_id, item.attempts) for item in delivered] == [(event.id, 1)]
    assert delivered[0].delay_seconds >= 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_a_database_error_inside_deliver_fails_only_its_event(db_outbox):
    """deliver() runs outside any transaction - its own failed statement fails its event, and the
    others of the batch are delivered and recorded."""
    failing_event = await DemoOutboxEvent.enqueue("bad", {})
    good_event = await DemoOutboxEvent.enqueue("good", {})
    delivered_topics: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        if event.topic == "bad":
            # Duplicate unique id - a real database error raised from inside deliver().
            await DemoOutboxEvent.objects.create(id=good_event.id, topic="duplicate", payload={})
        delivered_topics.append(event.topic)

    relay = OutboxRelay(DemoOutboxEvent, deliver, batch_size=10)
    await relay.poll_once()

    assert delivered_topics == ["good"]
    await failing_event.refresh_from_db()
    assert failing_event.attempts == 1
    assert failing_event.last_error
    await good_event.refresh_from_db()
    assert good_event.published_at is not None
    assert await DemoOutboxEvent.objects.filter(topic="duplicate").count() == 0


@pytest.mark.asyncio
async def test_a_delivery_running_out_of_time_fails(db_outbox):
    import asyncio

    event = await DemoOutboxEvent.enqueue("widget.updated", {})

    async def hangs(_event: DemoOutboxEvent) -> None:
        await asyncio.sleep(10)

    relay = OutboxRelay(DemoOutboxEvent, hangs, delivery_timeout_seconds=0.05, lease_seconds=5)
    await relay.poll_once()

    await event.refresh_from_db()
    assert event.attempts == 1
    assert event.published_at is None
