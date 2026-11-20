"""Recovery applies durable decisions without treating age or partial success as an abort."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hare.core.connections.connections import Connections
from hare.transactions import DistributedRecovery
from hare.transactions.atomic.atomic import Atomic
from hare.transactions.distributed.distributed_coordinator import DistributedCoordinator
from hare.transactions.distributed.distributed_decision_store import DistributedDecisionStore
from hare.transactions.enums import DistributedTransactionResolution

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_prepared_completion_invalidates_both_clients_before_fresh_client_closes(monkeypatch, commit, outcome):
    events = []
    error = {"success": None, "failure": ConnectionError("lost response"), "cancelled": asyncio.CancelledError()}[
        outcome
    ]

    async def execute(*args):
        events.append("statement")
        if error is not None:
            raise error

    async def invalidate(label):
        events.append(label)

    async def close():
        events.append("close")

    connection = SimpleNamespace(
        dialect=SimpleNamespace(two_phase_commit=Mock(), literals=Mock()),
        execute=execute,
        invalidate_schema_cache=lambda: invalidate("fresh"),
        close=close,
    )
    source = SimpleNamespace(
        invalidate_schema_cache=lambda: invalidate("source"),
        create_independent_client=Mock(return_value=connection),
    )
    client = SimpleNamespace(get_non_transactional_client=lambda: source)
    ambient = Mock(side_effect=AssertionError("prepared completion must use its original client"))
    monkeypatch.setattr(Connections, "current", ambient)
    operation = DistributedCoordinator._get_prepared_on_fresh_connection(client, "books", "migration", commit=commit)
    if error is None:
        await operation
    else:
        with pytest.raises(type(error)) as caught:
            await operation
        assert caught.value is error
    assert events == ["statement", "fresh", "source", "close"]
    source.create_independent_client.assert_called_once_with()
    ambient.assert_not_called()


@pytest.fixture
def recovery(monkeypatch):
    xid = DistributedCoordinator._make_xid("coordinator")
    state = SimpleNamespace(
        xid=xid,
        rows=[{"xid": xid, "participant_aliases": json.dumps(["first", "second"]), "is_stale": True}],
        prepared={"first", "second"},
        failures=set(),
        actions=[],
    )
    statements = Mock()
    statements.get_pending_decisions_sql.return_value = "decisions"
    statements.get_prepared_listing_sql.return_value = "prepared"
    statements.like_escape_character = "\\"
    clients = {}

    def make_execute(alias):
        async def execute(query, parameters=None):
            if query == "decisions":
                return None, state.rows
            if query == "prepared" and alias in state.prepared:
                return None, [{"gid": f"{xid}:{alias}"}]
            return None, []

        return execute

    for alias in ("coordinator", "first", "second"):
        clients[alias] = SimpleNamespace(
            is_transaction_client=False,
            dialect=SimpleNamespace(two_phase_commit=statements),
            features=SimpleNamespace(supports_two_phase_commit=True),
            get_shared_table_name=lambda table: table,
            execute=make_execute(alias),
        )
    monkeypatch.setattr(Atomic, "get_connection", clients.__getitem__)
    monkeypatch.setattr(Connections, "aliases", lambda: list(clients))

    async def is_prepared(client, alias, transaction_xid):
        return alias in state.prepared

    async def commit(client, alias, transaction_xid):
        state.actions.append((alias, DistributedTransactionResolution.COMMIT))
        if alias in state.failures:
            raise ConnectionError("participant unavailable")
        state.prepared.remove(alias)

    async def rollback(client, alias, transaction_xid):
        state.actions.append((alias, DistributedTransactionResolution.ROLLBACK))
        state.prepared.remove(alias)

    monkeypatch.setattr(DistributedCoordinator, "_xid_is_prepared", is_prepared)
    monkeypatch.setattr(DistributedCoordinator, "commit_prepared", commit)
    monkeypatch.setattr(DistributedCoordinator, "rollback_prepared", rollback)
    state.complete = AsyncMock()
    monkeypatch.setattr(DistributedDecisionStore, "complete", state.complete)
    return state


async def test_partial_recovery_keeps_decision_pending_and_rechecks_on_retry(recovery):
    recovery.failures.add("second")
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=0)
    assert [result.error is not None for result in report.attempts] == [False, True]
    assert recovery.prepared == {"second"}
    recovery.complete.assert_not_awaited()
    recovery.failures.clear()
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=0)
    assert [result.entry.participant_alias for result in report.attempts] == ["second"]
    assert not recovery.prepared
    assert report.completed_xids == (recovery.xid,)
    recovery.complete.assert_awaited_once_with(recovery.xid)


async def test_young_decision_protects_older_prepared_participants(recovery):
    recovery.rows[0]["is_stale"] = False
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=300)
    assert report.entries == ()
    assert recovery.actions == []
    assert recovery.prepared == {"first", "second"}
    recovery.complete.assert_not_awaited()


async def test_finishing_recovery_completes_log_after_all_participants_already_finished(recovery):
    recovery.prepared.clear()
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=0)
    assert report.entries == ()
    assert report.completed_xids == (recovery.xid,)
    recovery.complete.assert_awaited_once_with(recovery.xid)


async def test_decision_update_failure_is_reported_without_repeating_prepared_commits(recovery):
    failure = RuntimeError("decision update failed")
    recovery.complete.side_effect = failure
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=0)
    assert not recovery.prepared
    assert report.completed_xids == ()
    assert report.decision_errors == ((recovery.xid, failure),)
    recovery.complete.side_effect = None
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=0)
    assert report.attempts == ()
    assert report.completed_xids == (recovery.xid,)
    assert len(recovery.actions) == 2


async def test_detection_performs_no_recovery_action(recovery):
    report = await DistributedRecovery.run("coordinator", older_than_seconds=0)
    assert len(report.entries) == 2
    assert report.attempts == ()
    assert recovery.actions == []
    recovery.complete.assert_not_awaited()


async def test_foreign_decision_is_not_decoded_and_orphans_use_the_current_coordinator(recovery):
    recovery.rows = [{"xid": DistributedCoordinator._make_xid("foreign"), "participant_aliases": "foreign format"}]
    report = await DistributedRecovery.run("coordinator", finish=True, older_than_seconds=0)
    assert {entry.resolution for entry in report.entries} == {DistributedTransactionResolution.ROLLBACK}
    assert not recovery.prepared
