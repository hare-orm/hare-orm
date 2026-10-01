"""A cancelled or timed-out SQLite query is interrupted instead of running on in aiosqlite's
worker thread."""

import asyncio

import pytest

from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament

#: Runs for several seconds unless interrupted - far longer than LONG_QUERY_TEST_TIMEOUT_SECONDS.
LONG_SQLITE_QUERY = (
    "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter "
    "WHERE value < 40000000) SELECT count(*) FROM counter"
)
LONG_QUERY_TEST_TIMEOUT_SECONDS = 3


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_cancelled_query_is_interrupted(db_truncate):
    """A timed-out query used to keep running on aiosqlite's worker thread, holding the shared
    connection - every later query waited for it to finish on its own."""
    connection = Tournament._meta.db
    async with asyncio.timeout(LONG_QUERY_TEST_TIMEOUT_SECONDS):
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

    async with asyncio.timeout(LONG_QUERY_TEST_TIMEOUT_SECONDS):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(body(), 0.1)
        async with Transactions.atomic():
            await Tournament.objects.create(name="next")
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["next"]
