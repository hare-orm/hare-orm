"""A local transaction ending is distinct from its durable database result."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hare.contrib.test import requires_features
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.client.transaction_lifecycle.transaction_state import TransactionState
from hare.instrumentation.observers.observers import Observers
from hare.transactions import TransactionEventType, TransactionOutcome, TransactionPhase, Transactions
from tests.shared_models.testmodels import Tournament


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_cancelled_callback_does_not_skip_later_callbacks():
    failure = asyncio.CancelledError("callback cancelled")
    later = Mock()

    async def cancel():
        raise failure

    with pytest.raises(asyncio.CancelledError) as raised:
        await TransactionCallbacks.run_callbacks([cancel, later], "on_commit()")
    assert raised.value is failure
    later.assert_called_once()


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
async def test_callback_error_does_not_change_committed_outcome(db_truncate):
    failure = RuntimeError("callback failed")
    connection = None

    async def callback():
        assert connection.transaction_state.outcome is TransactionOutcome.COMMITTED
        assert connection.transaction_state.phase is TransactionPhase.FINISHED
        raise failure

    with pytest.raises(RuntimeError) as raised:
        async with Transactions.atomic() as connection:
            await Tournament.objects.create(name="committed before callback")
            Transactions.on_commit(callback)
    assert raised.value is failure
    assert connection.transaction_state.outcome is TransactionOutcome.COMMITTED
    assert await Tournament.objects.filter(name="committed before callback").exists()


@pytest.mark.asyncio
@requires_features(supports_transactions=True, supports_savepoints=True)
async def test_released_savepoint_is_not_a_committed_transaction(db_truncate):
    async with Transactions.atomic() as outer:
        async with Transactions.atomic() as inner:
            await Tournament.objects.create(name="released then rolled back")
        assert inner.transaction_state.outcome is TransactionOutcome.RELEASED
        assert outer.transaction_state.outcome is TransactionOutcome.PENDING
        await outer.rollback()
    assert outer.transaction_state.outcome is TransactionOutcome.ROLLED_BACK
    assert not await Tournament.objects.filter(name="released then rolled back").exists()


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_unknown_commit_reports_unknown_and_runs_no_callback(monkeypatch):
    commit_callback, rollback_callback = Mock(), Mock()
    events = []
    client = SimpleNamespace(
        transaction_state=TransactionState(),
        _transaction_context_reference=None,
        _on_commit_callbacks=[SimpleNamespace(callback=commit_callback)],
        _on_rollback_callbacks=[SimpleNamespace(callback=rollback_callback)],
        _transaction_started_at=time.perf_counter(),
        connection_alias="default",
        log=Mock(),
    )
    monkeypatch.setattr(Observers, "record_transaction", lambda *args: events.append(args))
    failure = ConnectionError("commit confirmation lost")
    await TransactionEnding.finish_after_lost_connection(client, failure, commit_outcome_unknown=True)
    assert client.transaction_state.phase is TransactionPhase.FINISHED
    assert client.transaction_state.outcome is TransactionOutcome.UNKNOWN
    assert events[0][0] is TransactionEventType.UNKNOWN
    assert events[0][3] is failure
    commit_callback.assert_not_called()
    rollback_callback.assert_not_called()
    assert not client._on_commit_callbacks and not client._on_rollback_callbacks


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_finished_driver_handle_does_not_prove_commit(monkeypatch):
    failure = RuntimeError("driver handle already finished")
    client = SimpleNamespace(
        transaction_state=TransactionState(TransactionPhase.ACTIVE),
        _has_begun=lambda: True,
        _before_top_level_end=AsyncMock(return_value=None),
        _begin_pending=False,
        _driver_commit=AsyncMock(side_effect=failure),
        _driver_rollback=AsyncMock(),
        _is_connection_lost=lambda error: False,
        _is_commit_rejection=lambda error: False,
        _is_transaction_finished_error=lambda error: error is failure,
        _do_nothing=lambda: None,
        _mark_top_level_operation_abandoned=Mock(),
        _transaction_context_reference=None,
        _on_commit_callbacks=[SimpleNamespace(callback=Mock())],
        _on_rollback_callbacks=[SimpleNamespace(callback=Mock())],
        _transaction_started_at=None,
    )
    monkeypatch.setattr(TransactionEnding, "hold_transaction_for_ending", AsyncMock(return_value=(None, None)))
    with pytest.raises(RuntimeError) as raised:
        await TransactionEnding.end_top_level(client, TransactionEventType.COMMIT)
    assert raised.value is failure
    assert client.transaction_state.outcome is TransactionOutcome.UNKNOWN
    assert not client._on_commit_callbacks and not client._on_rollback_callbacks
    client._driver_commit.assert_awaited_once()
    client._driver_rollback.assert_not_awaited()
