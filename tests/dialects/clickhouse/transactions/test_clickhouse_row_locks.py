"""select_for_update() on ClickHouse (``transactions=true`` with ``keeper_hosts``): the rows read are locked
in ClickHouse Keeper by their keys until the transaction ends - nowait, skip_locked, a wait for another
transaction's lock, of=, lock_timeout."""

import asyncio

import pytest

from hare.dialects.clickhouse.keeper.clickhouse_row_locks import ClickhouseRowLocks
from hare.exceptions import ConfigurationError, OperationalError, QueryError, TransactionRetryError
from hare.transactions import Transactions
from tests.dialects.clickhouse.transactions.models import Entry, Note


async def hold_row_locks(get_rows, locked, release, change=None):
    """Locks rows in a transaction of its own and holds them until ``release`` is set."""
    async with Transactions.atomic():
        rows = await get_rows()
        locked.set()
        await release.wait()
        if change is not None:
            await change()
    return rows


async def start_holding(get_rows, change=None):
    """Starts a transaction holding the locks of rows - once they are locked."""
    locked, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_row_locks(get_rows, locked, release, change))
    locking = asyncio.create_task(locked.wait())
    await asyncio.wait([locking, holder], timeout=10, return_when=asyncio.FIRST_COMPLETED)
    if holder.done():
        locking.cancel()
        await holder
    return holder, release


async def add_entries(count):
    await Entry.objects.bulk_create([Entry(id=number, account="a", amount=number) for number in range(1, count + 1)])


@pytest.mark.asyncio
async def test_the_connection_locks_rows_by_their_keys(clickhouse_row_locks_db):
    features = Entry._meta.connection.features
    assert features.supports_select_for_update and features.locks_rows_by_key


@pytest.mark.asyncio
async def test_a_locked_row_is_busy_for_another_transaction(clickhouse_row_locks_db):
    await add_entries(3)
    holder, release = await start_holding(lambda: Entry.objects.filter(id__in=[1, 2]).select_for_update())
    try:
        async with Transactions.atomic():
            with pytest.raises(OperationalError, match="nowait"):
                await Entry.objects.filter(id=2).select_for_update(nowait=True)
        async with Transactions.atomic():
            free = await Entry.objects.order_by("id").select_for_update(skip_locked=True)
            assert [entry.id for entry in free] == [3]
    finally:
        release.set()
    assert [entry.id for entry in await holder] == [1, 2]
    # Given back with the transaction.
    async with Transactions.atomic():
        assert len(await Entry.objects.select_for_update(nowait=True)) == 3


@pytest.mark.asyncio
async def test_skip_locked_fills_the_slice_with_free_rows(clickhouse_row_locks_db):
    await add_entries(6)
    holder, release = await start_holding(lambda: Entry.objects.filter(id__in=[1, 3]).select_for_update())
    try:
        async with Transactions.atomic():
            free = await Entry.objects.order_by("id").select_for_update(skip_locked=True)[1:3]
            assert [entry.id for entry in free] == [4, 5]
            free_ids = Entry.objects.order_by("id").select_for_update(skip_locked=True).values_list("id", flat=True)
            assert await free_ids == [2, 4, 5, 6]
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_a_lock_is_waited_for(clickhouse_row_locks_db):
    await add_entries(2)
    holder, release = await start_holding(lambda: Entry.objects.filter(id=1).select_for_update())

    async def lock_in_another_transaction():
        async with Transactions.atomic():
            return await Entry.objects.select_for_update().get(id=1)

    waiter = asyncio.create_task(lock_in_another_transaction())
    await asyncio.sleep(0.3)
    assert not waiter.done()
    release.set()
    await holder
    # Unchanged by the transaction that held it - read as it is.
    assert (await asyncio.wait_for(waiter, 10)).amount == 1


@pytest.mark.asyncio
async def test_a_row_changed_while_waited_for_ends_the_wait_with_a_retry(clickhouse_row_locks_db):
    await add_entries(2)

    async def change():
        await Entry.objects.filter(id=1).update(amount=100)

    holder, release = await start_holding(lambda: Entry.objects.filter(id=1).select_for_update(), change)

    async def lock_in_another_transaction():
        async with Transactions.atomic():
            return await Entry.objects.filter(id__in=[1, 2]).select_for_update().values_list("id", "amount")

    waiter = asyncio.create_task(lock_in_another_transaction())
    await asyncio.sleep(0.3)
    release.set()
    await holder
    # The transaction reads the row as it was when it began - it can't lock what it doesn't see.
    with pytest.raises(TransactionRetryError):
        await asyncio.wait_for(waiter, 10)
    async with Transactions.atomic():
        assert await Entry.objects.filter(id=1).select_for_update().values_list("amount", flat=True) == [100]


@pytest.mark.asyncio
async def test_the_locks_are_given_back_on_rollback(clickhouse_row_locks_db):
    await add_entries(1)
    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            await Entry.objects.select_for_update()
            raise RuntimeError("rolled back")
    async with Transactions.atomic():
        assert len(await Entry.objects.select_for_update(nowait=True)) == 1


@pytest.mark.asyncio
async def test_a_wait_ends_at_the_lock_timeout(clickhouse_row_locks_db):
    await add_entries(1)
    holder, release = await start_holding(lambda: Entry.objects.select_for_update())
    try:
        with pytest.raises(OperationalError, match="lock_timeout"):
            async with Transactions.atomic(lock_timeout=0.3):
                await Entry.objects.select_for_update()
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_of_locks_the_rows_of_a_relation(clickhouse_row_locks_db):
    await add_entries(2)
    await Note.objects.bulk_create([Note(id=1, entry_id=1, text="x"), Note(id=2, entry_id=2, text="y")])
    holder, release = await start_holding(
        lambda: Note.objects.filter(id=1).select_related("entry").select_for_update(of=("entry",))
    )
    try:
        async with Transactions.atomic():
            # The note itself isn't locked - its entry is.
            assert [note.id for note in await Note.objects.select_for_update(nowait=True)] == [1, 2]
            with pytest.raises(OperationalError, match="nowait"):
                await Entry.objects.filter(id=1).select_for_update(nowait=True)
            assert [entry.id for entry in await Entry.objects.filter(id=2).select_for_update(nowait=True)] == [2]
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_a_row_lock_outside_a_transaction_is_refused(clickhouse_row_locks_db):
    with pytest.raises(QueryError, match="atomic"):
        await Entry.objects.select_for_update()


@pytest.mark.asyncio
async def test_a_shared_row_lock_is_refused(clickhouse_row_locks_db):
    async with Transactions.atomic():
        with pytest.raises(Exception, match="share"):
            await Entry.objects.select_for_update(share=True)


@pytest.mark.asyncio
async def test_the_keeper_addresses_are_read(clickhouse_row_locks_db):
    assert ClickhouseRowLocks.get_addresses("keeper-1:9181, keeper-2") == (("keeper-1", 9181), ("keeper-2", 9181))
    with pytest.raises(ConfigurationError):
        ClickhouseRowLocks.get_addresses("keeper-1:port")


@pytest.mark.asyncio
async def test_streamed_rows_are_locked(clickhouse_row_locks_db):
    await add_entries(3)

    async def stream_locked():
        return [entry.id async for entry in Entry.objects.order_by("id").select_for_update().stream(chunk_size=2)]

    holder, release = await start_holding(stream_locked)
    try:
        async with Transactions.atomic():
            assert await Entry.objects.select_for_update(skip_locked=True) == []
    finally:
        release.set()
    assert await holder == [1, 2, 3]
