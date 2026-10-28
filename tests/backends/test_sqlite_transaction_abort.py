"""SQLite ends the whole transaction on its own when a write is interrupted (statement_timeout,
cancellation) or a trigger raises RAISE(ROLLBACK) - hare must treat it as aborted, not keep
running statements in autocommit mode."""

import asyncio
import time

import pytest

from hare.contrib.test import requires_features
from hare.dialects.base.client.transaction_lifecycle import transaction_ending
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_transaction_client import AiosqliteTransactionClient
from hare.exceptions import IntegrityError, OperationalError, TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.waiting import WAIT_DEADLINE_SECONDS

ENDLESS_COUNT_SQL = (
    "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter) SELECT count(*) FROM counter"
)
ENDLESS_WRITE_SQL = f"UPDATE tournament SET name = ({ENDLESS_COUNT_SQL}) WHERE id = ?"
# Finite (about two minutes uninterrupted), so a regression fails the test instead of hanging it.
LONG_COUNT_SQL = (
    "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000000) "
    "SELECT count(*) FROM counter"
)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
@pytest.mark.parametrize("nested", (False, True))
@pytest.mark.parametrize("catches_cancellation", (False, True))
@pytest.mark.parametrize("read_only", (False, True))
async def test_cancelled_deferred_begin_cannot_outlive_its_transaction(
    db_truncate, monkeypatch, nested, catches_cancellation, read_only
):
    monkeypatch.setattr(transaction_ending, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0)
    started, released, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_begin = AiosqliteTransactionClient._driver_send_begin
    connection = Tournament.get_connection(for_write=True)
    _, original_busy_timeout = await connection.execute("PRAGMA busy_timeout")

    async def delayed_begin(client):
        started.set()
        try:
            await released.wait()
            await original_begin(client)
        finally:
            completed.set()

    monkeypatch.setattr(AiosqliteTransactionClient, "_driver_send_begin", delayed_begin)

    async def write():
        async with Transactions.atomic(read_only=read_only, lock_timeout=0.2):
            try:
                if nested:
                    async with Transactions.atomic():
                        await Tournament.objects.create(name="cancelled")
                else:
                    await Tournament.objects.create(name="cancelled")
            except asyncio.CancelledError:
                if not catches_cancellation:
                    raise

    task = asyncio.create_task(write())
    try:
        await asyncio.wait_for(started.wait(), WAIT_DEADLINE_SECONDS)
        task.cancel()
        expected_error = (
            TransactionManagementError if catches_cancellation and not read_only else asyncio.CancelledError
        )
        with pytest.raises(expected_error):
            await asyncio.wait_for(task, WAIT_DEADLINE_SECONDS)
        released.set()
        await asyncio.wait_for(completed.wait(), WAIT_DEADLINE_SECONDS)
        await Tournament.objects.create(name="after cancellation")
        assert not connection._connection.in_transaction
        assert await Tournament.objects.filter(name="cancelled").count() == 0
        _, busy_timeout = await connection.execute("PRAGMA busy_timeout")
        assert busy_timeout == original_busy_timeout
    finally:
        released.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(completed.wait(), WAIT_DEADLINE_SECONDS)
        await connection._connection.rollback()


@pytest.mark.timing
@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_cancelled_query_inside_statement_timeout_transaction_is_interrupted(db_truncate):
    started_at = time.monotonic()
    with pytest.raises(TimeoutError):
        async with Transactions.atomic(statement_timeout=60) as connection:
            await asyncio.wait_for(connection.execute(LONG_COUNT_SQL), 0.2)
    assert time.monotonic() - started_at < 5
    await Tournament.objects.create(name="after cancel")
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_interrupted_write_aborts_transaction_instead_of_autocommitting(db_truncate):
    with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
        async with Transactions.atomic(statement_timeout=0.3) as connection:
            tournament = await Tournament.objects.create(name="before")
            with pytest.raises(OperationalError, match="rolled back the whole transaction"):
                await connection.execute(ENDLESS_WRITE_SQL, [tournament.pk])
            await Tournament.objects.create(name="after")
    assert await Tournament.objects.all().count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_interrupted_write_inside_savepoint_aborts_outer_transaction(db_truncate):
    rolled_back: list[str] = []
    with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
        async with Transactions.atomic(statement_timeout=0.3):
            tournament = await Tournament.objects.create(name="before")
            with pytest.raises(OperationalError, match="statement timeout"):
                async with Transactions.atomic() as inner:
                    Transactions.on_rollback(lambda: rolled_back.append("inner"))
                    await inner.execute(ENDLESS_WRITE_SQL, [tournament.pk])
            await Tournament.objects.create(name="after")
    assert rolled_back == ["inner"]
    assert await Tournament.objects.all().count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_commit_of_aborted_transaction_raises_and_commits_nothing(db_truncate):
    with pytest.raises(TransactionManagementError, match="nothing was committed"):
        async with Transactions.atomic(statement_timeout=0.3) as connection:
            tournament = await Tournament.objects.create(name="before")
            with pytest.raises(OperationalError):
                await connection.execute(ENDLESS_WRITE_SQL, [tournament.pk])
    assert await Tournament.objects.all().count() == 0
    await Tournament.objects.create(name="connection still usable")
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_trigger_raise_rollback_aborts_transaction(db_truncate):
    connection = Tournament._meta.connection
    await connection.execute_script(
        "CREATE TEMP TRIGGER hare_test_reject_rollback BEFORE INSERT ON tournament "
        "WHEN NEW.name = 'reject' BEGIN SELECT RAISE(ROLLBACK, 'rejected by trigger'); END"
    )
    try:
        with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
            async with Transactions.atomic():
                await Tournament.objects.create(name="before")
                with pytest.raises(IntegrityError, match="rejected by trigger"):
                    await Tournament.objects.create(name="reject")
                await Tournament.objects.create(name="after")
        assert await Tournament.objects.all().count() == 0
    finally:
        await connection.execute_script("DROP TRIGGER hare_test_reject_rollback")
