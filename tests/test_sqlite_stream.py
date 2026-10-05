"""stream() on SQLite: the rows are read off a cursor of the transaction's connection in batches of
chunk_size, never all at once; leaving the stream early or cancelling the task closes the cursor and
leaves the transaction usable."""

from __future__ import annotations

import asyncio

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteTransactionClient
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


@pytest.mark.asyncio
@requires_features(dialect="sqlite", supports_streaming=True)
async def test_rows_come_in_batches_of_chunk_size(db, monkeypatch):
    for index in range(1, 6):
        await Tournament.objects.create(id=index, name=f"t{index}")
    fetched_batch_sizes: list[int] = []
    fetch_stream_rows = AiosqliteTransactionClient._fetch_stream_rows

    async def counting_fetch(self, cursor, batch_size):
        rows = await fetch_stream_rows(self, cursor, batch_size)
        fetched_batch_sizes.append(len(rows))
        return rows

    monkeypatch.setattr(AiosqliteTransactionClient, "_fetch_stream_rows", counting_fetch)
    async with Transactions.atomic():
        ids = [tournament.id async for tournament in Tournament.objects.order_by("id").stream(chunk_size=2)]
    assert ids == [1, 2, 3, 4, 5]
    assert fetched_batch_sizes == [2, 2, 1, 0]


@pytest.mark.asyncio
@requires_features(dialect="sqlite", supports_streaming=True)
async def test_leaving_early_or_cancelling_keeps_the_transaction_usable(db):
    for index in range(1, 6):
        await Tournament.objects.create(id=index, name=f"t{index}")
    async with Transactions.atomic():
        async for tournament in Tournament.objects.order_by("id").stream(chunk_size=1):
            if tournament.id == 2:
                break
        # Another query between fetches, and after the stream was left.
        await Tournament.objects.create(id=6, name="t6")
        assert await Tournament.objects.count() == 6

        started = asyncio.Event()

        async def read_slowly() -> None:
            async for _tournament in Tournament.objects.order_by("id").stream(chunk_size=1):
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(read_slowly())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await Tournament.objects.count() == 6
    assert Connections.get("models").features.supports_streaming
