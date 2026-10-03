"""deliver() always raising -> attempts increments and last_error is populated on every attempt,
and once attempts reaches max_delivery_attempts the row is no longer claimed by future polls
(dead-lettered) even though published_at stays NULL forever - see docs/integrations/outbox.md for
how to query for these operationally.

SQLite is fine for this: OutboxRelay._poll_once() only issues SELECT ... FOR UPDATE SKIP LOCKED
on a backend that supports it (Postgres) - on SQLite it falls back to a plain claim query, which
is safe here because a single OutboxRelay instance (used throughout this whole test module) is
exactly the assumption that fallback relies on.
"""

import pytest

from hare.contrib.outbox import OutboxRelay
from tests.contrib.outbox.models import DemoOutboxEvent

MAX_DELIVERY_ATTEMPTS = 3


class _BrokenDelivery(Exception):
    pass


@pytest.mark.asyncio
async def test_attempts_and_last_error_accumulate_then_stop_being_reclaimed(db_outbox):
    event = await DemoOutboxEvent.publish(topic="widget.updated", payload={})

    async def always_fails(_event: DemoOutboxEvent) -> None:
        raise _BrokenDelivery("delivery backend unreachable")

    relay = OutboxRelay(DemoOutboxEvent, always_fails, max_delivery_attempts=MAX_DELIVERY_ATTEMPTS)

    for expected_attempts in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        claimed = await relay._poll_once()
        assert claimed == 1
        refreshed = await DemoOutboxEvent.objects.get(id=event.id)
        assert refreshed.attempts == expected_attempts
        assert "delivery backend unreachable" in refreshed.last_error
        assert refreshed.published_at is None

    # Dead-lettered: attempts has reached the cap, so it's no longer claimed at all.
    claimed_after_cap = await relay._poll_once()
    assert claimed_after_cap == 0
    final = await DemoOutboxEvent.objects.get(id=event.id)
    assert final.attempts == MAX_DELIVERY_ATTEMPTS
    assert final.published_at is None


@pytest.mark.asyncio
async def test_a_later_success_still_publishes_a_previously_failed_event(db_outbox):
    event = await DemoOutboxEvent.publish(topic="widget.updated", payload={})

    attempt_count = 0

    async def fails_once_then_succeeds(_event: DemoOutboxEvent) -> None:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise _BrokenDelivery("transient")

    relay = OutboxRelay(DemoOutboxEvent, fails_once_then_succeeds, max_delivery_attempts=MAX_DELIVERY_ATTEMPTS)
    await relay._poll_once()
    await relay._poll_once()

    refreshed = await DemoOutboxEvent.objects.get(id=event.id)
    assert refreshed.attempts == 1
    assert refreshed.published_at is not None


@pytest.mark.asyncio
async def test_a_database_error_inside_deliver_is_counted_and_does_not_block_the_queue(db_outbox):
    """On Postgres a failed statement inside deliver() used to abort the claim transaction, so
    recording attempts/last_error failed as well and the row stayed first in the queue forever."""
    failing_event = await DemoOutboxEvent.publish(topic="bad", payload={})
    good_event = await DemoOutboxEvent.publish(topic="good", payload={})
    delivered_topics: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        if event.topic == "bad":
            # Duplicate primary key - a real database error raised from inside deliver().
            await DemoOutboxEvent.objects.create(id=good_event.id, topic="duplicate", payload={})
        delivered_topics.append(event.topic)

    relay = OutboxRelay(DemoOutboxEvent, deliver, max_delivery_attempts=MAX_DELIVERY_ATTEMPTS, batch_size=10)
    for _ in range(MAX_DELIVERY_ATTEMPTS + 1):
        await relay._poll_once()

    assert delivered_topics == ["good"]
    refreshed_failing_event = await DemoOutboxEvent.objects.get(id=failing_event.id)
    assert refreshed_failing_event.attempts == MAX_DELIVERY_ATTEMPTS
    assert refreshed_failing_event.last_error
    assert refreshed_failing_event.published_at is None
    refreshed_good_event = await DemoOutboxEvent.objects.get(id=good_event.id)
    assert refreshed_good_event.published_at is not None
    assert await DemoOutboxEvent.objects.filter(topic="duplicate").count() == 0
