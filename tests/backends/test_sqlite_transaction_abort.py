"""SQLite ends the whole transaction on its own when a write is interrupted (statement_timeout,
cancellation) or a trigger raises RAISE(ROLLBACK) - hare must treat it as aborted, not keep
running statements in autocommit mode."""

import asyncio
import time

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import IntegrityError, OperationalError, TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament

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
    connection = Tournament._meta.db
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
