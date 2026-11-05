"""Postgres transactions shared by several tasks: a stream against a sibling's savepoints, and a
cancelled statement whose CancelRequest must never hit a later statement (rust_pg)."""

import asyncio
from contextlib import aclosing

import pytest

from hare.contrib.test import requires_features
from hare.dialects.base.client.transaction_lifecycle import transaction_ending
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.waiting import WAIT_DEADLINE_SECONDS

TEST_TIMEOUT_SECONDS = 60


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_discards_the_connection_of_an_abandoned_begin(db_truncate, monkeypatch):
    if "hare.dialects.postgresql.drivers.asyncpg" not in type(Tournament.get_connection()).__module__:
        pytest.skip("Only asyncpg sends its deferred BEGIN through PendingStatements")
    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgTransactionClient

    monkeypatch.setattr(transaction_ending, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0)
    started, released, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_begin = AsyncpgTransactionClient._driver_send_begin
    physical_connections = []

    async def delayed_begin(client):
        physical_connections.append(client._connection._con)
        started.set()
        try:
            await released.wait()
            await original_begin(client)
        finally:
            completed.set()

    monkeypatch.setattr(AsyncpgTransactionClient, "_driver_send_begin", delayed_begin)

    async def write():
        async with Transactions.atomic():
            await Tournament.objects.create(name="cancelled")

    task = asyncio.create_task(write())
    try:
        await asyncio.wait_for(started.wait(), WAIT_DEADLINE_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_DEADLINE_SECONDS)
        assert physical_connections[0].is_closed()
        released.set()
        await asyncio.wait_for(completed.wait(), WAIT_DEADLINE_SECONDS)
        await Tournament.objects.create(name="after cancellation")
        assert await Tournament.objects.count() == 1
    finally:
        released.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(completed.wait(), WAIT_DEADLINE_SECONDS)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stream_and_a_sibling_opening_savepoints_share_the_connection(db_truncate):
    await Tournament.objects.bulk_create([Tournament(name=f"tournament {index}") for index in range(3)])
    first_row_read = asyncio.Event()
    savepoint_closed = asyncio.Event()

    async def open_savepoint() -> None:
        await first_row_read.wait()
        async with Transactions.atomic():
            assert await Tournament.objects.count() == 3
        savepoint_closed.set()

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic():
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(open_savepoint())
                async with aclosing(Tournament.objects.all().order_by("id").stream(chunk_size=1)) as stream:
                    first = await anext(stream)
                    assert first.name == "tournament 0"
                    first_row_read.set()
                    await savepoint_closed.wait()
                    assert [item.name async for item in stream] == ["tournament 1", "tournament 2"]


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("boundary", ["open", "fetch"])
@pytest.mark.asyncio
async def test_stream_waits_outside_a_sibling_savepoint(db_truncate, monkeypatch, boundary):
    names = [f"tournament {index}" for index in range(3)]
    await Tournament.objects.bulk_create([Tournament(name=name) for name in names])
    ready, start_read, waiting_for_span = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_wait = NestedSavepointLock.wait_for_query_span

    async def read_stream():
        values = []
        async with aclosing(Tournament.objects.all().order_by("id").stream(chunk_size=1)) as stream:
            if boundary == "fetch":
                values.append((await anext(stream)).name)
            ready.set()
            await start_read.wait()
            values.extend([item.name async for item in stream])
        return values

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic() as connection:
            async with asyncio.TaskGroup() as tasks:
                reader = tasks.create_task(read_stream())

                async def observe_wait(lock, current_span):
                    if lock is connection._savepoint_lock and asyncio.current_task() is reader:
                        waiting_for_span.set()
                    return await original_wait(lock, current_span)

                monkeypatch.setattr(NestedSavepointLock, "wait_for_query_span", observe_wait)
                await ready.wait()
                with pytest.raises(ValueError, match="rollback sibling"):
                    async with Transactions.atomic():
                        assert await Tournament.objects.count() == 3
                        start_read.set()
                        waiting = tasks.create_task(waiting_for_span.wait())
                        completed, _ = await asyncio.wait((reader, waiting), return_when=asyncio.FIRST_COMPLETED)
                        assert waiting in completed
                        assert not reader.done()
                        raise ValueError("rollback sibling")
                assert await reader == names
                assert await Tournament.objects.count() == 3


#: How many outer transactions the stray-cancel stress test runs - before the fix roughly one in
#: thirty of them was aborted by a stray CancelRequest.
STRAY_CANCEL_ROUNDS = 120


class Boom(Exception):
    """Fails a TaskGroup so it cancels its still-running workers."""


@requires_features(dialect="postgresql")
@pytest.mark.timing
@pytest.mark.asyncio
async def test_cancelled_statements_never_cancel_a_later_statement_of_the_transaction(db_truncate):
    """Workers cancelled by a failing TaskGroup inside their own savepoints: a statement cancelled
    just as it finishes sends a CancelRequest (over a new connection) that used to reach the
    server only after a later statement of the same transaction had started - and cancel that
    one instead, aborting the outer transaction that handled the failure."""

    async def worker(round_number: int, worker_number: int) -> None:
        while True:
            async with Transactions.atomic():
                await Tournament.objects.create(name=f"worker {round_number}.{worker_number}")

    async def boom(round_number: int) -> None:
        await asyncio.sleep(0.005 + (round_number % 10) * 0.003)
        raise Boom

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS * 2):
        for round_number in range(STRAY_CANCEL_ROUNDS):
            async with Transactions.atomic():
                await Tournament.objects.create(name=f"outer {round_number}")
                with pytest.raises(ExceptionGroup):
                    async with asyncio.TaskGroup() as task_group:
                        for worker_number in range(4):
                            task_group.create_task(worker(round_number, worker_number))
                        task_group.create_task(boom(round_number))
                await Tournament.objects.create(name=f"after {round_number}")
    assert await Tournament.objects.filter(name__startswith="after ").count() == STRAY_CANCEL_ROUNDS


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_an_idle_pooled_connection_is_taken_without_waiting(db_truncate):
    """rust_pg: checking out a pooled connection for a transaction went to the tokio runtime and
    back through the event loop even when one sat idle - it is taken on the spot now, only a wait
    for one is awaited."""
    from hare import Connections

    client = Connections.get("models")
    try_acquire = getattr(client._pool, "try_acquire", None)
    if try_acquire is None:
        pytest.skip("a driver or rust.pg build without try_acquire()")
    await Tournament.objects.create(name="warm the pool")
    acquiring = client._pool_acquire()
    with pytest.raises(StopIteration) as stopped:
        acquiring.send(None)
    connection = stopped.value.value
    assert connection is not None
    await client._pool.release(connection)
    async with Transactions.atomic():
        await Tournament.objects.create(name="inside")
    assert await Tournament.objects.filter(name="inside").exists()
