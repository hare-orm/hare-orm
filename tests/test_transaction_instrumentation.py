import asyncio

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.instrumentation.declarations import TransactionEvent
from hare.instrumentation.observers.observer_dispatch import ObserverDispatch
from hare.instrumentation.observers.observers import Observers
from hare.transactions.enums import TransactionEventType
from hare.transactions.transactions import Transactions
from tests.testmodels import IntFields


@pytest.fixture(autouse=True)
def _clear_observers():
    """The process observers are process-wide state - a test that leaves an observer behind would
    leak it into later tests."""
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()
    yield
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()


@pytest.mark.asyncio
async def test_observe_and_get_transaction_event():
    calls = []

    def observer(event):
        calls.append((event.type, event.connection_alias, event.duration_ms, event.error))

    Observers.observe(TransactionEvent, observer)

    Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)

    assert calls == [(TransactionEventType.BEGIN, "default", 0.0, None)]


@pytest.mark.asyncio
async def test_unobserve_stops_further_calls():
    calls = []

    def observer(event):
        calls.append(event.type)

    Observers.observe(TransactionEvent, observer)
    Observers.unobserve(TransactionEvent, observer)

    Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)

    assert calls == []


def test_unobserve_is_idempotent():
    Observers.unobserve(TransactionEvent, lambda event: None)


def test_record_transaction_without_observers_builds_no_event(monkeypatch):
    """Every real transaction reports twice - with no observer, no event is even built."""

    def fail(*args, **kwargs):
        raise AssertionError("an event was given out although nothing observes it")

    monkeypatch.setattr(Observers, "notify", fail)
    Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)


@pytest.mark.asyncio
async def test_a_raising_observer_is_logged_and_isolated(caplog):
    def bad_observer(event):
        raise RuntimeError("observer exploded")

    calls = []
    Observers.observe(TransactionEvent, bad_observer)
    Observers.observe(TransactionEvent, lambda event: calls.append(event.type))

    with caplog.at_level("ERROR", logger="hare"):
        Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)

    assert calls == [TransactionEventType.BEGIN]
    assert any("raised" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_async_observer_sees_begin_before_commit_even_when_slow():
    calls = []

    async def observer(event):
        if event.type is TransactionEventType.BEGIN:
            await asyncio.sleep(0.05)
        calls.append(event.type)

    Observers.observe(TransactionEvent, observer)

    Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)
    Observers.record_transaction(TransactionEventType.COMMIT, "default", 1.0, None)
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@pytest.mark.asyncio
async def test_the_next_event_still_arrives_when_the_previous_observer_call_raised():
    calls = []

    async def observer(event):
        calls.append(event.type)
        if event.type is TransactionEventType.BEGIN:
            raise RuntimeError("observer failed")

    Observers.observe(TransactionEvent, observer)

    Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)
    Observers.record_transaction(TransactionEventType.ROLLBACK, "default", 1.0, None)
    with pytest.raises(RuntimeError, match="observer failed"):
        await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]


@pytest.mark.asyncio
async def test_transactions_an_observer_runs_itself_report_no_events():
    """A transaction observer that opens its own transaction doesn't observe it - it would
    re-trigger itself on every BEGIN it caused."""
    async with hare_test_context(["tests.testmodels"]):
        events = []

        async def audit_observer(event):
            events.append(event.type)
            async with Transactions.atomic():
                await IntFields.objects.create(intnum=len(events))

        Observers.observe(TransactionEvent, audit_observer)
        try:
            async with Transactions.atomic():
                await IntFields.objects.create(intnum=0)
            await Observers.wait_for_pending()
            await asyncio.sleep(0.1)
            await Observers.wait_for_pending()
        finally:
            Observers.unobserve(TransactionEvent, audit_observer)

        assert events == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]
        assert await IntFields.objects.all().count() == 3


@pytest.mark.asyncio
async def test_wait_for_pending_cancels_a_stuck_transaction_observer_after_the_timeout():
    async def stuck_observer(event):
        await asyncio.Event().wait()

    Observers.observe(TransactionEvent, stuck_observer)
    Observers.record_transaction(TransactionEventType.BEGIN, "default", 0.0, None)

    await asyncio.wait_for(Observers.wait_for_pending(timeout_seconds=0.1), 5)

    assert ObserverDispatch.pending_tasks == set()
