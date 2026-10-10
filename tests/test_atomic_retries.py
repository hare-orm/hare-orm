"""Transactions.atomic(retries=N): a transaction run again after TransactionRetryError - as a decorator, as
`async for attempt in atomic(retries=N)`, for a real serialization failure of two concurrent serializable
transactions - and the transactions it never runs again."""

from __future__ import annotations

import asyncio

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import QueryError, TransactionRetryError
from hare.query.functions import Sum
from hare.transactions.atomic.atomic import Atomic
from hare.transactions.constants import MAX_TRANSACTION_RETRIES
from hare.transactions.transactions import Transactions
from tests.testmodels import IntFields


def failing_runs(failures: int) -> list[int]:
    """A run counter; the first ``failures`` runs raise ``TransactionRetryError`` after a write."""
    return [0, failures]


async def run_once(runs: list[int], intnum: int) -> str:
    runs[0] += 1
    await IntFields.objects.create(intnum=intnum)
    if runs[0] <= runs[1]:
        raise TransactionRetryError("could not serialize access")
    return "done"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_decorated_function_runs_again(db_truncate):
    runs = failing_runs(2)

    @Transactions.atomic(retries=2)
    async def write() -> str:
        return await run_once(runs, 1)

    assert await write() == "done"
    assert runs[0] == 3
    # The failed runs were rolled back.
    assert await IntFields.objects.count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_block_runs_again(db_truncate):
    runs = failing_runs(1)
    async for attempt in Transactions.atomic(retries=3):
        async with attempt:
            await run_once(runs, 2)
    assert runs[0] == 2
    assert await IntFields.objects.count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_run_not_entered_ends_the_iteration(db_truncate):
    attempts = []
    with pytest.raises(QueryError, match=r"A run of atomic\(retries=...\) wasn't entered"):
        async for attempt in Transactions.atomic(retries=3):
            attempts.append(attempt)
    assert len(attempts) == 1
    entered_after_skip = []
    with pytest.raises(QueryError, match="wasn't entered"):
        async for attempt in Transactions.atomic(retries=3):
            if not entered_after_skip:
                entered_after_skip.append(attempt)
                continue
    assert len(entered_after_skip) == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_commit_failing_to_serialize_runs_again(db_truncate, monkeypatch):
    commits = [0]
    commit = Atomic.__aexit__

    async def failing_first_commit(self, exc_type, exc_val, exc_tb):
        commits[0] += 1
        if exc_val is None and commits[0] == 1:
            # Rolled back, as a failed COMMIT leaves it.
            await commit(self, TransactionRetryError, TransactionRetryError("commit"), None)
            raise TransactionRetryError("could not serialize access at commit")
        return await commit(self, exc_type, exc_val, exc_tb)

    monkeypatch.setattr(Atomic, "__aexit__", failing_first_commit)
    runs = failing_runs(0)
    async for attempt in Transactions.atomic(retries=1):
        async with attempt:
            await run_once(runs, 6)
    assert runs[0] == 2
    assert await IntFields.objects.count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_no_run_left_raises_the_last_error(db_truncate):
    runs = failing_runs(5)

    @Transactions.atomic(retries=2)
    async def write() -> str:
        return await run_once(runs, 3)

    with pytest.raises(TransactionRetryError):
        await write()
    assert runs[0] == 3
    assert await IntFields.objects.count() == 0
    runs = failing_runs(1)
    with pytest.raises(TransactionRetryError):
        async for attempt in Transactions.atomic():
            async with attempt:
                await run_once(runs, 4)
    assert runs[0] == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_another_error_and_a_nested_transaction_never_run_again(db_truncate):
    runs = [0]

    @Transactions.atomic(retries=3)
    async def fail() -> None:
        runs[0] += 1
        raise ValueError("not a retry")

    with pytest.raises(ValueError, match="not a retry"):
        await fail()
    assert runs == [1]
    nested_runs = failing_runs(1)

    @Transactions.atomic(retries=3)
    async def nested_write() -> str:
        return await run_once(nested_runs, 5)

    with pytest.raises(TransactionRetryError):
        async with Transactions.atomic():
            await nested_write()
    # Only the outer transaction can run again.
    assert nested_runs[0] == 1
    assert await IntFields.objects.count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_two_serializable_transactions_reading_what_the_other_writes(db_truncate):
    await IntFields.objects.create(intnum=0)
    runs = [0]
    both_read = asyncio.Event()
    # SQLite runs one writing transaction at a time - only PostgreSQL's run side by side.
    runs_concurrently = Connections.get("models").dialect.name == "postgresql"
    readers = [0]

    @Transactions.atomic(isolation="serializable", retries=5)
    async def add_total() -> None:
        runs[0] += 1
        total = (await IntFields.objects.aggregate(total=Sum("intnum")))["total"]
        readers[0] += 1
        if readers[0] == 2:
            both_read.set()
        # Both read before either writes on the first runs - a write skew a serializable
        # transaction refuses to commit.
        if runs_concurrently and runs[0] <= 2:
            await asyncio.wait_for(both_read.wait(), 5)
        await IntFields.objects.create(intnum=total + 1)

    await asyncio.gather(add_total(), add_total())
    assert await IntFields.objects.count() == 3
    if runs_concurrently:
        # One of them read a total the other changed - it ran again on the new one.
        assert runs[0] == 3
        assert sorted(await IntFields.objects.values_list("intnum", flat=True)) == [0, 1, 2]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_plain_block_with_retries_is_refused(db_truncate):
    with pytest.raises(QueryError, match="runs a block again as `async for attempt in atomic"):
        async with Transactions.atomic(retries=2):
            pass


@pytest.mark.parametrize("retries", [-1, MAX_TRANSACTION_RETRIES + 1, True, 1.0, "2"])
def test_a_wrong_retries_is_refused(retries):
    with pytest.raises(QueryError, match="atomic\\(retries=...\\) takes an int from 0 to"):
        Transactions.atomic(retries=retries)
