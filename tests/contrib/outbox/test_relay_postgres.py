"""Postgres-only coverage: LISTEN/NOTIFY lower-latency delivery (and stop()'s cleanup of the
background tasks it spawns), and the SELECT ... FOR UPDATE SKIP LOCKED half of concurrent-relay
row claiming. All of it requires a real Postgres server - requires_features(dialect="postgresql")
skips cleanly under SQLite (the default HARE_TEST_DB) rather than failing.
"""

import asyncio
from typing import Any
from unittest import mock

import pytest

from hare.contrib.notify import NotificationListener
from hare.contrib.outbox import OutboxRelay
from hare.contrib.test import requires_features
from tests.contrib.outbox.models import DemoOutboxEvent, TenantScopedOutboxEvent


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_notify_delivers_faster_than_the_poll_interval(db_outbox):
    """poll_interval_seconds is set far longer than this test's own timeout, so a delivery
    inside that timeout can only have come from the LISTEN/NOTIFY path, not the polling
    backstop."""
    event = await DemoOutboxEvent.publish(topic="widget.updated", payload={})

    delivered = asyncio.Event()

    async def deliver(_event: DemoOutboxEvent) -> None:
        delivered.set()

    channel = "hare_test_outbox_relay_listen"
    relay = OutboxRelay(
        DemoOutboxEvent,
        deliver,
        poll_interval_seconds=30,
        listen_channel=channel,
    )
    async with relay:
        await asyncio.sleep(0.2)  # give the LISTEN subscription time to actually connect
        await DemoOutboxEvent._meta.db.execute(f"NOTIFY {channel}")
        await asyncio.wait_for(delivered.wait(), timeout=5)

    refreshed = await DemoOutboxEvent.objects.get(id=event.id)
    assert refreshed.published_at is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_loop_backs_off_when_connection_dies_before_becoming_healthy(db_outbox):
    """A LISTEN connection that dies (listener.is_closed() flips True) before ever surviving one
    health-check interval, without db.listen() itself ever raising - e.g. an idle-session
    timeout/NAT/firewall killing the dedicated LISTEN connection outright, over and over - used
    to redial immediately with ZERO backoff and no cap: `attempt` was reset to 0 unconditionally
    right after a successful db.listen(), regardless of whether the connection ever actually
    became healthy, so this scenario never even reached the exponential-backoff/give-up logic
    that already existed for a db.listen() call that raises outright."""

    class DeadOnArrivalListener:
        def is_closed(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    connect_count = 0

    async def fake_listen(channel: str, callback: object) -> DeadOnArrivalListener:
        nonlocal connect_count
        connect_count += 1
        return DeadOnArrivalListener()

    sleeps: list[float] = []

    async def fast_pause(self: NotificationListener, delay: float) -> None:
        sleeps.append(delay)

    with (
        mock.patch.object(DemoOutboxEvent._meta.db, "listen", fake_listen),
        mock.patch.object(NotificationListener, "pause", fast_pause),
    ):
        relay = OutboxRelay(DemoOutboxEvent, lambda event: None, listen_channel="dead_on_arrival")
        await relay._run_listen_loop()

    # LISTEN_MAX_RECONNECT_ATTEMPTS = 5 - the initial connect plus 5 backoff-and-retry cycles
    # (matching the pre-existing db.listen()-raises branch's identical off-by-one), then give up.
    assert connect_count == 6
    # A real backoff sleep (nonzero delay) happened before every redial, not an immediate one.
    assert len(sleeps) == 5
    assert all(delay > 0 for delay in sleeps)
    # Exponential: strictly increasing, not the same flat delay every time.
    assert sleeps == sorted(sleeps)
    assert sleeps[-1] > sleeps[0]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stop_cancels_a_notify_triggered_background_poll_task(db_outbox):
    """Regression test: stop() used to cancel/await only self._task and self._listen_task,
    never self._background_tasks - the set _on_notify() adds a fresh poll task to every time a
    NOTIFY fires. A NOTIFY that fired shortly before stop() was called used to leave its spawned
    task running untracked after stop() returned, contradicting stop()'s own docstring ("waits
    for [the relay's background tasks] to finish cleanly")."""
    entered_delivery = asyncio.Event()
    hold_open = asyncio.Event()

    async def deliver_and_hold(_event: DemoOutboxEvent) -> None:
        entered_delivery.set()
        await hold_open.wait()

    channel = "hare_test_outbox_relay_stop_cancels_background_tasks"
    relay = OutboxRelay(
        DemoOutboxEvent,
        deliver_and_hold,
        poll_interval_seconds=30,  # long enough that only the NOTIFY-spawned task can act in this test
        listen_channel=channel,
    )
    await relay.start()
    # Let the relay's own immediate first (empty - nothing published yet) poll cycle run and go
    # to sleep for poll_interval_seconds, and the LISTEN subscription actually connect, BEFORE
    # publishing - otherwise that first cycle can race the NOTIFY below and claim the row itself,
    # leaving _background_tasks empty instead of exercising the NOTIFY-triggered path this test
    # is actually about.
    await asyncio.sleep(0.2)

    await DemoOutboxEvent.publish(topic="widget.updated", payload={})
    await DemoOutboxEvent._meta.db.execute(f"NOTIFY {channel}")
    await asyncio.wait_for(entered_delivery.wait(), timeout=5)  # the NOTIFY-spawned task is now mid-delivery

    background_task = next(iter(relay._background_tasks))
    assert not background_task.done()

    await relay.stop()  # must not raise, and must actually finish off the background task too

    assert background_task.done()
    assert len(relay._background_tasks) == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_skip_locked_never_double_claims_the_row_a_concurrent_relay_is_delivering(db_outbox):
    """Deterministic proof of the SKIP LOCKED half of safe concurrent-relay claiming.

    Each row is claimed and delivered inside its OWN short transaction (see
    OutboxRelay._claim_and_deliver_one()) rather than the whole batch being claimed and locked
    in one go - only the single row currently being delivered is locked at any moment. While
    relay_a's transaction still holds that one row locked (deliver() hasn't returned, so the
    transaction hasn't committed), a concurrent relay_b poll against the same table must skip
    exactly that row - claiming and delivering every OTHER row instead - rather than blocking on
    it, which SELECT ... FOR UPDATE alone (no SKIP LOCKED) would do."""
    published_events = [await DemoOutboxEvent.publish(topic="widget.updated", payload={"i": i}) for i in range(5)]

    entered_delivery = asyncio.Event()
    hold_open = asyncio.Event()
    held_event_id: dict[str, Any] = {}

    async def deliver_and_hold(event: DemoOutboxEvent) -> None:
        held_event_id["id"] = event.id
        entered_delivery.set()
        await hold_open.wait()

    relay_a = OutboxRelay(DemoOutboxEvent, deliver_and_hold, batch_size=5)
    poll_a_task = asyncio.create_task(relay_a._poll_once())
    await asyncio.wait_for(entered_delivery.wait(), timeout=5)  # relay_a now holds exactly one row locked

    delivered_by_b: list[Any] = []

    async def track_delivery(event: DemoOutboxEvent) -> None:
        delivered_by_b.append(event.id)

    relay_b = OutboxRelay(DemoOutboxEvent, track_delivery, batch_size=5)
    claimed_by_b = await asyncio.wait_for(relay_b._poll_once(), timeout=5)
    assert claimed_by_b == 4  # every row except the one relay_a is still delivering
    assert held_event_id["id"] not in delivered_by_b

    hold_open.set()
    claimed_by_a = await asyncio.wait_for(poll_a_task, timeout=5)
    assert claimed_by_a == 1

    published_count = await DemoOutboxEvent.objects.filter(published_at__isnull=False).count()
    assert published_count == len(published_events)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_failed_save_on_one_event_does_not_roll_back_earlier_events_in_the_same_batch(db_outbox):
    """Regression test for a bug where _poll_once() wrapped the WHOLE claimed batch in one
    transaction: if a later event's own event.save() call (the "mark as delivered" write)
    raised - a transient DB error, or the polling task being cancelled mid-save - the exception
    used to propagate out of that ONE shared transaction, rolling back every earlier event's
    already-successful delivery too, even though deliver() had already fired its real side
    effect for them (silent duplicate delivery on the next poll).

    Each row is now claimed, delivered, and saved inside its own transaction
    (OutboxRelay._claim_and_deliver_one()), so a later row's save failure can only roll back
    that one row - earlier rows in the same polling cycle already committed independently and
    must not be redelivered."""
    events = [await DemoOutboxEvent.publish(topic="widget.updated", payload={"i": i}) for i in range(3)]
    failing_event_id = events[1].id

    delivered_ids: list[Any] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered_ids.append(event.id)

    relay = OutboxRelay(DemoOutboxEvent, deliver, batch_size=3)

    original_save = DemoOutboxEvent.save

    async def save_that_fails_for_the_second_event(self: DemoOutboxEvent, *args: Any, **kwargs: Any) -> None:
        if self.id == failing_event_id:
            raise RuntimeError("simulated transient DB error while marking delivered")
        await original_save(self, *args, **kwargs)

    with mock.patch.object(DemoOutboxEvent, "save", save_that_fails_for_the_second_event):
        with pytest.raises(RuntimeError, match="simulated transient DB error"):
            await relay._poll_once()

    # deliver()'s real side effect already fired for the first two events - the bug is about
    # the SECOND write (marking delivered) not being durable, not about deliver() itself.
    assert delivered_ids == [events[0].id, events[1].id]

    refreshed_first = await DemoOutboxEvent.objects.get(id=events[0].id)
    assert refreshed_first.published_at is not None  # committed on its own, survived event 2's failure

    refreshed_second = await DemoOutboxEvent.objects.get(id=events[1].id)
    assert refreshed_second.published_at is None  # its own save() failed - rolled back
    assert refreshed_second.attempts == 0  # rolled back too - not treated as an ordinary retry

    # A later poll must not re-deliver event 1 (already durably published) - only event 2
    # (genuinely still unpublished) and event 3 (never even attempted this cycle) get processed.
    delivered_ids.clear()
    second_poll_claimed = await relay._poll_once()
    assert second_poll_claimed == 2
    assert sorted(delivered_ids) == sorted([events[1].id, events[2].id])

    refreshed_first_again = await DemoOutboxEvent.objects.get(id=events[0].id)
    assert refreshed_first_again.published_at == refreshed_first.published_at  # untouched, not redelivered


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_claim_and_deliver_one_spans_every_tenant_when_model_has_tenant_field(db_outbox):
    """The SELECT ... FOR UPDATE SKIP LOCKED claim path (_claim_and_deliver_one(), only
    reachable on a backend with support_for_update - Postgres) must service every tenant's rows
    the same way the SQLite fallback in _poll_once() does - see
    test_relay_polling.py's identical-in-spirit test for that path."""
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=1)
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=2)

    delivered: list[int] = []

    async def deliver(event: TenantScopedOutboxEvent) -> None:
        delivered.append(event.tenant_id)

    relay = OutboxRelay(TenantScopedOutboxEvent, deliver, batch_size=10)
    claimed = await relay._poll_once()

    assert claimed == 2
    assert sorted(delivered) == [1, 2]
