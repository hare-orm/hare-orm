"""start()/stop() idempotency and clean cancellation, the relay started inside a transaction, and
the wakeup: a signal after commit - never for a rollback - starts a poll at once, the signals of one
transaction joined."""

import asyncio

import pytest

from hare.contrib.outbox import InProcessWakeup, OutboxRelay
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import DemoOutboxEvent


async def no_op_deliver(_event: DemoOutboxEvent) -> None:
    pass


async def wait_until(condition, timeout: float = 5) -> None:
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the condition never held")


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, poll_interval_seconds=0.05)
    await relay.stop()
    await relay.start()
    first_task = relay.task
    await relay.start()
    assert relay.task is first_task
    await asyncio.sleep(0.1)
    await relay.stop()
    await relay.stop()
    assert relay.task is None


@pytest.mark.asyncio
async def test_the_context_manager_stops_on_an_exception(db_outbox):
    class BusinessError(Exception):
        pass

    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, poll_interval_seconds=0.05)
    with pytest.raises(BusinessError):
        async with relay:
            assert relay.task is not None
            raise BusinessError("boom")
    assert relay.task is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_relay_started_inside_a_transaction_does_not_poll_through_it(db_outbox):
    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.topic)

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05)
    try:
        async with Transactions.atomic():
            await relay.start()
        await DemoOutboxEvent.enqueue("widget.after_transaction", {})
        await wait_until(lambda: delivered)
    finally:
        await relay.stop()

    assert delivered == ["widget.after_transaction"]


@pytest.mark.asyncio
async def test_stop_right_after_a_delivery_still_marks_it_published(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.updated", {})
    delivered = asyncio.Event()

    async def deliver(_event: DemoOutboxEvent) -> None:
        delivered.set()

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05)
    await relay.start()
    await asyncio.wait_for(delivered.wait(), timeout=5)
    await relay.stop()

    await event.refresh_from_db()
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_a_wakeup_delivers_long_before_the_poll_interval(db_outbox):
    wakeup = InProcessWakeup()
    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.topic)

    async with OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=60, wakeup=wakeup):
        await asyncio.sleep(0.05)
        await DemoOutboxEvent.enqueue("widget.created", {}, wakeup=wakeup)
        await wait_until(lambda: delivered)

    assert delivered == ["widget.created"]
    assert wakeup.callbacks == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_signals_wait_for_the_commit_and_join_per_transaction(db_outbox):
    wakeup = InProcessWakeup()
    signals: list[frozenset[str]] = []
    await wakeup.subscribe(signals.append, "models")
    try:
        async with Transactions.atomic():
            await DemoOutboxEvent.enqueue("widget.created", {}, wakeup=wakeup)
            await DemoOutboxEvent.enqueue("widget.created", {}, wakeup=wakeup)
            await DemoOutboxEvent.enqueue("widget.updated", {}, wakeup=wakeup)
            assert signals == []
        assert signals == [frozenset({"widget.created", "widget.updated"})]

        with pytest.raises(RuntimeError):
            async with Transactions.atomic():
                await DemoOutboxEvent.enqueue("widget.deleted", {}, wakeup=wakeup)
                raise RuntimeError("rolled back")
        assert len(signals) == 1

        await DemoOutboxEvent.enqueue("widget.outside", {}, wakeup=wakeup)
        assert signals[-1] == frozenset({"widget.outside"})
    finally:
        await wakeup.unsubscribe(signals.append)


@pytest.mark.asyncio
async def test_a_signal_of_other_topics_doesnt_wake_a_relay_of_its_topics(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, topics=["widget.updated"])
    relay.on_wakeup(frozenset({"gadget.updated"}))
    assert not relay.woken.is_set()
    relay.on_wakeup(frozenset({"gadget.updated", "widget.updated"}))
    assert relay.woken.is_set()
