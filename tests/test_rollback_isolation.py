"""RollbackIsolation throws away everything a block writes: a rolled-back transaction on each
connection, emptied tables where the database has no transactions; it refuses a commit of its
transaction and hands out the on_commit() callbacks that would never run."""

import pytest

from hare.contrib.test import RollbackIsolation
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.exceptions import TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


class RollBack(Exception):
    pass


@pytest.mark.asyncio
async def test_what_the_block_writes_is_gone_after_it(db_module):
    before = await Tournament.objects.count()
    async with RollbackIsolation(db_module) as context:
        assert HareContext.require_current() is context
        await Tournament.objects.create(id=901, name="isolated")
        assert await Tournament.objects.filter(id=901).exists()
    assert await Tournament.objects.count() == before
    assert not await Tournament.objects.filter(id=901).exists()


@pytest.mark.asyncio
async def test_a_transaction_of_the_block_is_a_savepoint_of_the_isolating_one(db_module):
    async with RollbackIsolation(db_module):
        if not Connections.get("models").features.supports_transactions:
            pytest.skip("the database has no transactions")
        async with Transactions.atomic():
            await Tournament.objects.create(id=902, name="kept-until-the-end")
        with pytest.raises(RollBack):
            async with Transactions.atomic():
                await Tournament.objects.create(id=903, name="rolled-back-at-once")
                raise RollBack()
        assert await Tournament.objects.filter(id=902).exists()
        assert not await Tournament.objects.filter(id=903).exists()
    assert not await Tournament.objects.filter(id__in=[902, 903]).exists()


@pytest.mark.asyncio
async def test_committing_the_isolating_transaction_is_refused(db_module):
    async with RollbackIsolation(db_module):
        connection = Connections.get("models")
        if not connection.features.supports_transactions:
            pytest.skip("the database has no transactions")
        await Tournament.objects.create(id=904, name="not-committed")
        with pytest.raises(TransactionManagementError, match="isolating a test"):
            await connection.commit()
    assert not await Tournament.objects.filter(id=904).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("execute", [False, True])
async def test_capture_on_commit_hands_out_the_callbacks(db_module, execute):
    ran: list[str] = []

    async def asynchronous_callback() -> None:
        ran.append("async")
        Transactions.on_commit(lambda: ran.append("registered by a callback"))

    isolation = RollbackIsolation(db_module)
    async with isolation:
        if not Connections.get("models").features.supports_transactions:
            pytest.skip("the database has no transactions")
        async with isolation.capture_on_commit(execute=execute) as callbacks:
            Transactions.on_commit(lambda: ran.append("sync"))
            async with Transactions.atomic():
                Transactions.on_commit(asynchronous_callback)
            # Nothing runs before the block exits.
            assert ran == []
    if execute:
        assert ran == ["sync", "async", "registered by a callback"]
        assert len(callbacks) == 3
    else:
        assert ran == []
        assert len(callbacks) == 2


@pytest.mark.asyncio
async def test_a_database_without_transactions_has_its_tables_emptied(db_module):
    async with RollbackIsolation(db_module) as context:
        await Tournament.objects.create(id=905, name="emptied-or-rolled-back")
        assert (
            context.get_connection().features.supports_transactions or await Tournament.objects.filter(id=905).exists()
        )
    assert not await Tournament.objects.filter(id=905).exists()
