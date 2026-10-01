"""start()/stop() idempotency and clean cancellation - OutboxRelay._run() runs as a plain
asyncio.Task; stop() cancels it and awaits it inside a try/except asyncio.CancelledError, so a
caller's own stop() call must never see CancelledError leak out, and calling start()/stop()
more than once must be a safe no-op rather than raising or spawning a second background task."""

import asyncio

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.contrib.test import requires_features
from tests.contrib.outbox.models import DemoOutboxEvent


async def _no_op_deliver(_event: DemoOutboxEvent) -> None:
    pass


@pytest.mark.asyncio
async def test_start_is_idempotent(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, _no_op_deliver, poll_interval_seconds=0.05)
    await relay.start()
    first_task = relay._task
    await relay.start()

    assert relay._task is first_task
    await relay.stop()


@pytest.mark.asyncio
async def test_stop_cancels_cleanly_with_no_leaked_cancelled_error(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, _no_op_deliver, poll_interval_seconds=0.05)
    await relay.start()
    await asyncio.sleep(0.1)  # let at least one poll cycle run

    await relay.stop()  # must not raise asyncio.CancelledError

    assert relay._task is None


@pytest.mark.asyncio
async def test_stop_is_idempotent(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, _no_op_deliver, poll_interval_seconds=0.05)
    await relay.start()
    await relay.stop()
    await relay.stop()  # must not raise

    assert relay._task is None


@pytest.mark.asyncio
async def test_stop_without_start_is_a_no_op(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, _no_op_deliver)
    await relay.stop()  # must not raise
    assert relay._task is None


@pytest.mark.asyncio
async def test_context_manager_stops_on_normal_exit(db_outbox):
    async with OutboxRelay(DemoOutboxEvent, _no_op_deliver, poll_interval_seconds=0.05) as relay:
        assert relay._task is not None
        await asyncio.sleep(0.05)

    assert relay._task is None


@pytest.mark.asyncio
async def test_context_manager_stops_on_exception(db_outbox):
    class _BusinessError(Exception):
        pass

    relay_ref: OutboxRelay | None = None
    with pytest.raises(_BusinessError):
        async with OutboxRelay(DemoOutboxEvent, _no_op_deliver, poll_interval_seconds=0.05) as relay:
            relay_ref = relay
            assert relay._task is not None
            raise _BusinessError("boom")

    assert relay_ref is not None
    assert relay_ref._task is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_relay_started_inside_a_transaction_does_not_poll_through_it(db_outbox):
    """The relay outlives the transaction start() is called in - its polling must use the alias's
    shared client, not the transaction's (finalised once the block ends)."""
    from hare.transactions.transactions import Transactions

    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.topic)

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05)
    try:
        async with Transactions.atomic():
            await relay.start()
        await DemoOutboxEvent.publish(topic="widget.after_transaction", payload={})
        for _ in range(100):
            if delivered:
                break
            await asyncio.sleep(0.05)
    finally:
        await relay.stop()

    assert delivered == ["widget.after_transaction"]


@pytest.mark.asyncio
async def test_a_burst_of_notifies_runs_one_poll_at_a_time(db_outbox):
    """Every NOTIFY used to start its own full poll cycle, so a burst of N NOTIFYs ran N
    concurrent polls."""
    for index in range(3):
        await DemoOutboxEvent.publish(topic="widget.updated", payload={"index": index})
    delivered_ids: list[object] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered_ids.append(event.id)

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=30, batch_size=2)
    original_poll_once = relay._poll_once
    running_polls = 0
    peak_running_polls = 0
    started_polls = 0

    async def counting_poll_once() -> int:
        nonlocal running_polls, peak_running_polls, started_polls
        running_polls += 1
        started_polls += 1
        peak_running_polls = max(peak_running_polls, running_polls)
        try:
            await asyncio.sleep(0.01)
            return await original_poll_once()
        finally:
            running_polls -= 1

    relay._poll_once = counting_poll_once  # type: ignore[method-assign]
    for _ in range(50):
        relay._on_notify("")
    assert len(relay._background_tasks) == 1
    await asyncio.wait_for(asyncio.gather(*relay._background_tasks), timeout=5)

    assert peak_running_polls == 1
    # A full first batch (2 of 3 rows) makes one more pass, which drains the queue.
    assert started_polls == 2
    assert len(delivered_ids) == 3


@pytest.mark.asyncio
async def test_a_notify_during_a_poll_makes_exactly_one_more_pass(db_outbox):
    await DemoOutboxEvent.publish(topic="widget.updated", payload={})
    relay = OutboxRelay(DemoOutboxEvent, _no_op_deliver, poll_interval_seconds=30, batch_size=10)
    original_poll_once = relay._poll_once
    started_polls = 0

    async def poll_once_with_notifies_arriving() -> int:
        nonlocal started_polls
        started_polls += 1
        if started_polls == 1:
            for _ in range(10):
                relay._on_notify("")
        return await original_poll_once()

    relay._poll_once = poll_once_with_notifies_arriving  # type: ignore[method-assign]
    relay._on_notify("")
    await asyncio.wait_for(asyncio.gather(*relay._background_tasks), timeout=5)

    assert started_polls == 2
    assert await DemoOutboxEvent.objects.filter(published_at__isnull=True).count() == 0
