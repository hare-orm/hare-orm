"""start()/stop() idempotency and clean cancellation, the relay started inside a transaction, and
the wakeup: a signal after commit - never for a rollback - starts a poll at once, the signals of one
transaction joined."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from hare.contrib.outbox import InProcessWakeup, OutboxRelay
from hare.contrib.outbox.relay.claimed_batch import ClaimedBatch
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import DemoOutboxEvent
from tests.contrib.outbox.wakeup_signals import signal_until_arrived
from tests.utils.waiting import WAIT_DEADLINE_SECONDS, wait_until


async def no_op_deliver(_event: DemoOutboxEvent) -> None:
    pass


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["subscription", "context", "listener"])
async def test_outbox_work_and_release_failures_are_both_retained(monkeypatch, boundary):
    failure, release_failure = RuntimeError("work"), RuntimeError("release")
    wakeup = InProcessWakeup()
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, wakeup=wakeup)

    if boundary == "subscription":
        relay._store = SimpleNamespace(
            client=SimpleNamespace(
                features=SimpleNamespace(
                    supports_row_updates=True, supports_returning=True, supports_correlated_subqueries=True
                )
            )
        )
        monkeypatch.setattr(wakeup, "subscribe", AsyncMock(side_effect=failure))
        monkeypatch.setattr(wakeup, "unsubscribe", AsyncMock(side_effect=release_failure))
        operation = relay.start()
    elif boundary == "context":
        monkeypatch.setattr(relay, "stop", AsyncMock(side_effect=release_failure))
        operation = relay.__aexit__(type(failure), failure, None)
    else:

        async def fail():
            raise failure

        listener = asyncio.create_task(fail())
        await asyncio.gather(listener, return_exceptions=True)
        monkeypatch.setattr(wakeup, "close", AsyncMock(side_effect=release_failure))
        operation = wakeup._stop_listening(listener)
    with pytest.raises(BaseExceptionGroup) as captured:
        await operation
    assert captured.value.exceptions == (failure, release_failure)


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stop", [False, True])
async def test_new_subscription_waits_for_the_previous_transport_to_close(monkeypatch, cancel_stop):
    wakeup = InProcessWakeup()
    closing, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def close():
        closing.set()
        await release.wait()
        closed.append(True)

    monkeypatch.setattr(wakeup, "close", close)
    first, second = lambda topics: None, lambda topics: None
    await wakeup.subscribe(first, "models")
    listener = wakeup.listen_task
    stopping = asyncio.create_task(wakeup.unsubscribe(first))
    await asyncio.wait_for(closing.wait(), WAIT_DEADLINE_SECONDS)
    starting = asyncio.create_task(wakeup.subscribe(second, "models"))
    try:
        for _ in range(2):
            if cancel_stop:
                stopping.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
        assert not starting.done()
        assert wakeup.listen_task is listener
        assert wakeup.subscriptions == []
    finally:
        release.set()
        outcomes = await asyncio.gather(stopping, starting, return_exceptions=True)
    assert isinstance(outcomes[0], asyncio.CancelledError) if cancel_stop else outcomes[0] is None
    assert outcomes[1] is None
    assert closed == [True]
    assert wakeup.listen_task is not listener
    await wakeup.unsubscribe(second)
    assert closed == [True, True]
    assert wakeup.listen_task is None


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("through_stop", [False, True])
async def test_repeated_cancellation_finishes_the_claimed_batch(monkeypatch, through_stop):
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, wakeup=InProcessWakeup())
    batch = ClaimedBatch(uuid4(), (DemoOutboxEvent(id=uuid4(), topic="book.created", payload={}),))
    entered, release = asyncio.Event(), asyncio.Event()
    recorded = []

    async def deliver_and_record(claimed):
        entered.set()
        await release.wait()
        recorded.append(claimed.token)

    monkeypatch.setattr(relay, "claim", AsyncMock(return_value=batch))
    monkeypatch.setattr(relay, "deliver_and_record", deliver_and_record)
    close = AsyncMock()
    monkeypatch.setattr(relay.delivery, "close", close)
    poll = asyncio.create_task(relay.poll_once())
    await asyncio.wait_for(entered.wait(), WAIT_DEADLINE_SECONDS)
    if through_stop:
        relay.task = poll
        waiter = asyncio.create_task(relay.stop())
    else:
        waiter = poll
    try:
        for _ in range(2):
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            waiter.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not waiter.done()
        assert recorded == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, WAIT_DEADLINE_SECONDS)
        assert recorded == [batch.token]
        if through_stop:
            close.assert_awaited_once()
            assert relay.task is None
    finally:
        release.set()
        await asyncio.gather(waiter, poll, return_exceptions=True)


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("unsubscribe_fails", [False, True])
async def test_concurrent_stops_release_each_resource_once(monkeypatch, unsubscribe_fails):
    wakeup = InProcessWakeup()
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver, wakeup=wakeup)
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("unsubscribe failed")

    async def unsubscribe(_callback):
        entered.set()
        await release.wait()
        if unsubscribe_fails:
            raise failure

    unsubscribe_call = AsyncMock(side_effect=unsubscribe)
    close = AsyncMock()
    monkeypatch.setattr(wakeup, "unsubscribe", unsubscribe_call)
    monkeypatch.setattr(relay.delivery, "close", close)
    relay.task = asyncio.create_task(asyncio.Event().wait())
    first = asyncio.create_task(relay.stop())
    second = asyncio.create_task(relay.stop())
    try:
        await asyncio.wait_for(entered.wait(), WAIT_DEADLINE_SECONDS)
        assert not first.done() and not second.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert outcomes == ([failure, failure] if unsubscribe_fails else [None, None])
    unsubscribe_call.assert_awaited_once_with(relay.on_wakeup)
    close.assert_awaited_once()
    assert relay.task is None
    await relay.stop()
    close.assert_awaited_once()


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
