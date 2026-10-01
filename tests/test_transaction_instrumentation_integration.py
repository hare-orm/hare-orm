"""End-to-end tests for TransactionEvent against a real Transactions.atomic() block - unlike
tests/test_transaction_instrumentation.py's own unit tests (which call
Observers.record_transaction() directly), these prove the actual begin()/commit()/rollback()
wiring in each backend fires the right events, exactly once, including through nesting."""

import asyncio
import time

import pytest

from hare.contrib.test import requires_features
from hare.instrumentation.observers import Observers
from hare.instrumentation.transaction_event import TransactionEvent
from hare.transactions.enums import TransactionEventType
from hare.transactions.transactions import Transactions


@pytest.fixture(autouse=True)
def _clear_observers():
    Observers.process_observers.clear()
    yield
    Observers.process_observers.clear()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_successful_transaction_fires_begin_then_commit(db_module):
    calls = []

    def hook(event):
        calls.append(event.type)

    Observers.observe(TransactionEvent, hook)

    async with Transactions.atomic():
        pass
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_transaction_fires_begin_then_rollback(db_module):
    calls = []

    def hook(event):
        calls.append(event.type)

    Observers.observe(TransactionEvent, hook)

    with pytest.raises(ValueError):
        async with Transactions.atomic():
            raise ValueError("boom")
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_transaction_fires_exactly_one_begin_commit_pair(db_module):
    """The key invariant this feature depends on: a savepoint (nested transaction) is not a real
    transaction lifecycle event - atomic() inside atomic() must fire BEGIN/COMMIT
    exactly once, for the outer transaction only, not once per nesting level."""
    calls = []

    def hook(event):
        calls.append(event.type)

    Observers.observe(TransactionEvent, hook)

    async with Transactions.atomic() as outer:
        async with outer._in_transaction():
            pass
        async with outer._in_transaction():
            pass
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_transaction_rollback_does_not_fire_a_rollback_event(db_module):
    """A savepoint that itself rolls back (but the outer transaction goes on to commit normally)
    must not fire ROLLBACK - only the outer transaction's own real commit/rollback counts."""
    calls = []

    def hook(event):
        calls.append(event.type)

    Observers.observe(TransactionEvent, hook)

    async with Transactions.atomic() as outer:
        with pytest.raises(ValueError):
            async with outer._in_transaction():
                raise ValueError("boom")
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_committed_transaction_reports_a_nonnegative_duration(db_module):
    calls = []

    def hook(event):
        event, connection_name, duration_ms = event.type, event.connection_name, event.duration_ms
        calls.append((event, connection_name, duration_ms))

    Observers.observe(TransactionEvent, hook)

    async with Transactions.atomic():
        pass
    await Observers.wait_for_pending()

    begin_event, begin_connection, begin_duration = calls[0]
    assert begin_event is TransactionEventType.BEGIN
    assert begin_duration == 0.0

    commit_event, commit_connection, commit_duration = calls[1]
    assert commit_event is TransactionEventType.COMMIT
    assert commit_connection == begin_connection
    assert commit_duration >= 0.0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failing_on_commit_callback_still_fires_commit_event(db_module):
    calls = []

    def hook(event):
        calls.append(event.type)

    def failing_callback():
        raise RuntimeError("callback failed")

    Observers.observe(TransactionEvent, hook)

    with pytest.raises(RuntimeError, match="callback failed"):
        async with Transactions.atomic():
            Transactions.on_commit(failing_callback)
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failing_on_rollback_callback_still_fires_rollback_event(db_module):
    calls = []

    def hook(event):
        calls.append(event.type)

    def failing_callback():
        raise RuntimeError("callback failed")

    Observers.observe(TransactionEvent, hook)

    with pytest.raises(ValueError, match="boom"):
        async with Transactions.atomic():
            Transactions.on_rollback(failing_callback)
            raise ValueError("boom")
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_slow_begin_hook_still_sees_begin_before_commit(db_module):
    """Each event's plain-function hook call runs in its own worker thread; a BEGIN call delayed
    by thread scheduling (a busy machine) used to be overtaken by the COMMIT call."""
    calls = []

    def hook(event):
        if event.type is TransactionEventType.BEGIN:
            time.sleep(0.05)
        calls.append(event.type)

    def failing_callback():
        raise RuntimeError("callback failed")

    Observers.observe(TransactionEvent, hook)

    with pytest.raises(RuntimeError, match="callback failed"):
        async with Transactions.atomic():
            Transactions.on_commit(failing_callback)
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_slow_begin_hook_still_sees_begin_before_rollback(db_module):
    calls = []

    async def hook(event):
        if event.type is TransactionEventType.BEGIN:
            await asyncio.sleep(0.05)
        calls.append(event.type)

    Observers.observe(TransactionEvent, hook)

    with pytest.raises(ValueError, match="boom"):
        async with Transactions.atomic():
            raise ValueError("boom")
    await Observers.wait_for_pending()

    assert calls == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]
