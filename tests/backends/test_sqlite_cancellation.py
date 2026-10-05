"""A cancelled or timed-out SQLite query is interrupted instead of running on in aiosqlite's
worker thread."""

import asyncio

import pytest

from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.waiting import WAIT_DEADLINE_SECONDS

#: Never ends unless interrupted - a query left running holds the connection for good.
LONG_SQLITE_QUERY = (
    "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter) SELECT count(*) FROM counter"
)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_cancelled_query_is_interrupted(db_truncate):
    """A timed-out query used to keep running on aiosqlite's worker thread, holding the shared
    connection - every later query waited for it to finish on its own."""
    connection = Tournament._meta.connection
    async with asyncio.timeout(WAIT_DEADLINE_SECONDS):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connection.execute(LONG_SQLITE_QUERY), 0.1)
        tasks = [asyncio.ensure_future(connection.execute(LONG_SQLITE_QUERY)) for _ in range(3)]
        await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert await Tournament.objects.all().count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_cancelled_query_inside_a_transaction_is_interrupted(db_truncate):
    async def body() -> None:
        async with Transactions.atomic() as connection:
            await Tournament.objects.create(name="rolled back")
            await connection.execute(LONG_SQLITE_QUERY)

    async with asyncio.timeout(WAIT_DEADLINE_SECONDS):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(body(), 0.1)
        async with Transactions.atomic():
            await Tournament.objects.create(name="next")
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["next"]
