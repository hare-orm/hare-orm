"""A cancelled or timed-out SQLite query is interrupted instead of running on in aiosqlite's
worker thread."""

import asyncio

import pytest

from hare.contrib.test import requires_features
from hare.core.resource_completion import ResourceCompletion
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.sqlite_progress_signal import SqliteProgressSignal
from tests.utils.waiting import WAIT_DEADLINE_SECONDS, wait_until

#: Never ends unless interrupted - a query left running holds the connection for good.
LONG_SQLITE_QUERY = (
    "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter) SELECT count(*) FROM counter"
)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_cancelled_query_is_interrupted(db_truncate):
    """Cancel one statement executing in SQLite and two queued behind its connection lock."""
    connection = Tournament._meta.connection
    await Tournament.objects.count()
    async with asyncio.timeout(WAIT_DEADLINE_SECONDS):
        async with SqliteProgressSignal(connection._connection) as progress:
            tasks = [asyncio.create_task(connection.execute(LONG_SQLITE_QUERY))]
            try:
                await progress.started.wait()
                tasks.extend(asyncio.create_task(connection.execute(LONG_SQLITE_QUERY)) for _ in range(2))
                await wait_until(lambda: connection.pool_statistics.acquiring == 2)
                for task in tasks:
                    task.cancel()
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await ResourceCompletion.wait(asyncio.gather(*tasks, return_exceptions=True))
        assert await Tournament.objects.count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_cancelled_query_inside_a_transaction_is_interrupted(db_truncate):
    connection = Tournament._meta.connection
    await Tournament.objects.count()
    progress = SqliteProgressSignal(connection._connection)

    async def body() -> None:
        async with Transactions.atomic() as transaction:
            await Tournament.objects.create(name="rolled back")
            async with progress:
                await transaction.execute(LONG_SQLITE_QUERY)

    async with asyncio.timeout(WAIT_DEADLINE_SECONDS):
        task = asyncio.create_task(body())
        try:
            await progress.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await ResourceCompletion.wait(asyncio.gather(task, return_exceptions=True))
        async with Transactions.atomic():
            await Tournament.objects.create(name="next")
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["next"]


@requires_features(dialect="sqlite")
@pytest.mark.timing
@pytest.mark.asyncio
@pytest.mark.parametrize("in_transaction", [False, True])
async def test_query_timeout_releases_the_sqlite_connection(db_truncate, in_transaction):
    async def body() -> None:
        if in_transaction:
            async with Transactions.atomic() as transaction:
                await Tournament.objects.create(name="rolled back")
                await transaction.execute(LONG_SQLITE_QUERY)
        else:
            await Tournament._meta.connection.execute(LONG_SQLITE_QUERY)

    async with asyncio.timeout(WAIT_DEADLINE_SECONDS):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(body(), 0.1)
        async with Transactions.atomic():
            await Tournament.objects.create(name="next")
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["next"]
