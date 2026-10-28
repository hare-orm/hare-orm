"""start()/stop() idempotency and clean cancellation, the relay started inside a transaction, and
the wakeup: a signal after commit - never for a rollback - starts a poll at once, the signals of one
transaction joined."""

import asyncio
import threading

import pytest

from hare.contrib.outbox import InProcessWakeup, OutboxRelay
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import DemoOutboxEvent
from tests.contrib.outbox.wakeup_signals import signal_until_arrived
from tests.utils.waiting import WAIT_DEADLINE_SECONDS, wait_until


async def no_op_deliver(_event: DemoOutboxEvent) -> None:
    pass


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, poll_interval_seconds=0.05)
    await relay.stop()
    await relay.start()
    first_task = relay.task
    await relay.start()
    assert relay.task is first_task
    for _ in range(10):
        await asyncio.sleep(0)
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
        await DemoOutboxEvent.enqueue("widget.created", {}, wakeup=wakeup)
        # Its signal is lost while the relay still subscribes - the next one wakes it.
        await signal_until_arrived(wakeup, "widget.created", lambda: delivered)

    assert delivered == ["widget.created"]
    assert wakeup.subscriptions == []


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


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_signal_survives_a_savepoint_rolled_back_before_another_event(db_outbox):
    wakeup = InProcessWakeup()
    signals: list[frozenset[str]] = []
    await wakeup.subscribe(signals.append, "models")
    try:
        async with Transactions.atomic():
            with pytest.raises(RuntimeError):
                async with Transactions.atomic():
                    await DemoOutboxEvent.enqueue("widget.created", {}, wakeup=wakeup)
                    raise RuntimeError("rolled back")
            await DemoOutboxEvent.enqueue("widget.updated", {}, wakeup=wakeup)
            async with Transactions.atomic():
                await DemoOutboxEvent.enqueue("widget.deleted", {}, wakeup=wakeup)
        # The rolled back savepoint's topic is left out, the rest joined into one signal.
        assert signals == [frozenset({"widget.updated", "widget.deleted"})]
        assert len(wakeup.pending_topics) == 0
    finally:
        await wakeup.unsubscribe(signals.append)


@pytest.mark.asyncio
async def test_a_signal_of_other_topics_doesnt_wake_a_relay_of_its_topics(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, topics=["widget.updated"])
    relay.on_wakeup(frozenset({"gadget.updated"}))
    assert not relay.woken.is_set()
    relay.on_wakeup(frozenset({"gadget.updated", "widget.updated"}))
    assert relay.woken.is_set()


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_signal_of_another_thread_calls_the_relay_on_the_event_loop_it_subscribed_in():
    """A write of a thread with an event loop of its own called the relay's callback in that thread - its
    asyncio.Event isn't thread-safe, and a relay's idle loop slept on to the next poll."""
    wakeup = InProcessWakeup()
    called = asyncio.Event()
    calls: list[tuple[int, frozenset[str]]] = []

    def callback(topics: frozenset[str]) -> None:
        calls.append((threading.get_ident(), topics))
        called.set()

    await wakeup.subscribe(callback, "models")
    try:
        thread = threading.Thread(target=lambda: asyncio.run(wakeup.signal(frozenset({"widget.created"}))))
        thread.start()
        await asyncio.wait_for(called.wait(), WAIT_DEADLINE_SECONDS)
        thread.join()
    finally:
        await wakeup.unsubscribe(callback)

    assert calls == [(threading.get_ident(), frozenset({"widget.created"}))]


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("debug", (False, True))
async def test_the_last_subscriber_can_stop_a_wakeup_from_another_event_loop(monkeypatch, debug):
    wakeup = InProcessWakeup()
    owner_loop = asyncio.get_running_loop()
    previous_debug = owner_loop.get_debug()
    owner_loop.set_debug(debug)
    closed_on = []

    async def close_transport():
        closed_on.append(asyncio.get_running_loop())

    monkeypatch.setattr(wakeup, "close", close_transport)

    def first_callback(_topics):
        pass

    def second_callback(_topics):
        pass

    await wakeup.subscribe(first_callback, "models")
    listener = wakeup.listen_task
    subscribed, released = threading.Event(), threading.Event()
    errors = []

    async def second_subscriber():
        try:
            await wakeup.subscribe(second_callback, "models")
            subscribed.set()
            await asyncio.to_thread(released.wait, WAIT_DEADLINE_SECONDS)
            await wakeup.unsubscribe(second_callback)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=lambda: asyncio.run(second_subscriber()))
    thread.start()
    try:
        assert await asyncio.to_thread(subscribed.wait, WAIT_DEADLINE_SECONDS)
        await wakeup.unsubscribe(first_callback)
        released.set()
        await asyncio.to_thread(thread.join, WAIT_DEADLINE_SECONDS)
        assert not thread.is_alive()
        assert errors == []
        assert listener.done()
        assert closed_on == [owner_loop]
    finally:
        owner_loop.set_debug(previous_debug)
        released.set()
        await asyncio.to_thread(thread.join, WAIT_DEADLINE_SECONDS)
        if listener is not None and not listener.done():
            listener.cancel()
        if listener is not None:
            await asyncio.gather(listener, return_exceptions=True)
