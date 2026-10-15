"""bulk_create(use_copy=True) inside Transactions.atomic() runs COPY on the transaction's own
connection on both PostgreSQL drivers: the rows commit and roll back with the transaction, a
savepoint's rollback included; a failed or cancelled COPY leaves the transaction aborted."""

import asyncio

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import IntegrityError, TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.testmodels import UniqueName


class RollBack(Exception):
    pass


def make_rows(prefix: str, count: int) -> list[UniqueName]:
    return [UniqueName(name=f"{prefix}{number}") for number in range(count)]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_copy_commits_and_rolls_back_with_the_transaction(db_truncate):
    async with Transactions.atomic():
        await UniqueName.objects.bulk_create(make_rows("kept", 30), use_copy=True, batch_size=7)
        # The transaction sees its own rows.
        assert await UniqueName.objects.filter(name__startswith="kept").count() == 30
    assert await UniqueName.objects.filter(name__startswith="kept").count() == 30

    with pytest.raises(RollBack):
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create(make_rows("gone", 5), use_copy=True)
            raise RollBack()
    assert await UniqueName.objects.filter(name__startswith="gone").count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_copy_as_the_first_statement_of_the_transaction(db_truncate):
    async with Transactions.atomic():
        await UniqueName.objects.bulk_create(make_rows("first", 3), use_copy=True)
        await UniqueName.objects.create(name="after-copy")
    assert await UniqueName.objects.filter(name__startswith="first").count() == 3
    assert await UniqueName.objects.filter(name="after-copy").exists()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_copy_inside_a_savepoint_rolls_back_with_it(db_truncate):
    async with Transactions.atomic():
        await UniqueName.objects.bulk_create(make_rows("outer", 2), use_copy=True)
        with pytest.raises(RollBack):
            async with Transactions.atomic():
                await UniqueName.objects.bulk_create(make_rows("inner", 4), use_copy=True)
                raise RollBack()
        assert await UniqueName.objects.filter(name__startswith="inner").count() == 0
    assert await UniqueName.objects.filter(name__startswith="outer").count() == 2
    assert await UniqueName.objects.filter(name__startswith="inner").count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_failed_copy_aborts_the_transaction(db_truncate):
    await UniqueName.objects.create(name="taken")
    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create(make_rows("ok", 2), use_copy=True)
            with pytest.raises(IntegrityError):
                await UniqueName.objects.bulk_create([UniqueName(name="taken")], use_copy=True)
            with pytest.raises(TransactionManagementError):
                await UniqueName.objects.count()
            raise IntegrityError("the transaction is aborted")
    assert await UniqueName.objects.filter(name__startswith="ok").count() == 0
    # The connection is usable afterwards.
    async with Transactions.atomic():
        await UniqueName.objects.bulk_create(make_rows("later", 2), use_copy=True)
    assert await UniqueName.objects.filter(name__startswith="later").count() == 2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_cancelled_copy_leaves_nothing_behind(db_truncate):
    async def copy_many_rows() -> None:
        async with Transactions.atomic():
            await UniqueName.objects.bulk_create(make_rows("many", 20000), use_copy=True)

    task = asyncio.create_task(copy_many_rows())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    count = await UniqueName.objects.filter(name__startswith="many").count()
    # Either the COPY finished and committed before the cancellation landed, or nothing of it stayed.
    assert count in (0, 20000)
    async with Transactions.atomic():
        await UniqueName.objects.bulk_create(make_rows("after-cancel", 2), use_copy=True)
    assert await UniqueName.objects.filter(name__startswith="after-cancel").count() == 2
