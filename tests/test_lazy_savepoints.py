"""A nested transaction's SAVEPOINT goes out ahead of the first statement run under it - a nested
block running no statement sends nothing, and its rollback still runs its callbacks."""

from unittest.mock import patch

import pytest

from hare.contrib.test import requires_features
from hare.dialects.base.client import TransactionClient
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


def spy_on_savepoints():
    """Records the name of every SAVEPOINT sent - by the client of the database under test."""
    sent_names: list[str] = []

    def spying_send(client_class):
        original = client_class._driver_savepoint

        async def send(self, name):
            sent_names.append(name)
            return await original(self, name)

        return patch.object(client_class, "_driver_savepoint", send)

    return sent_names, spying_send


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_nested_block_without_statements_sends_no_savepoint(db_truncate):
    sent_names, spying_send = spy_on_savepoints()
    async with Transactions.atomic() as outer:
        with spying_send(type(outer)):
            async with Transactions.atomic():
                async with Transactions.atomic():
                    pass
            assert sent_names == []
            async with Transactions.atomic() as middle:
                async with Transactions.atomic() as inner:
                    await Tournament.objects.create(name="inside")
            # Both enclosing savepoints went out, the outer one first.
            assert sent_names == [middle._savepoint_name, inner._savepoint_name]
    assert await Tournament.objects.filter(name="inside").exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_rolled_back_block_undoes_its_statements_whenever_its_savepoint_went_out(db_truncate):
    fired: list[str] = []

    class Boom(Exception):
        pass

    async with Transactions.atomic():
        await Tournament.objects.create(name="kept")
        with pytest.raises(Boom):
            async with Transactions.atomic():
                Transactions.on_rollback(lambda: fired.append("empty block"))
                raise Boom
        with pytest.raises(Boom):
            async with Transactions.atomic():
                await Tournament.objects.create(name="undone")
                Transactions.on_rollback(lambda: fired.append("block with a statement"))
                raise Boom
    assert fired == ["empty block", "block with a statement"]
    assert [tournament.name for tournament in await Tournament.objects.order_by("id")] == ["kept"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_transaction_client_keeps_no_pending_savepoint_once_its_blocks_end(db_truncate):
    async with Transactions.atomic() as outer:
        async with Transactions.atomic():
            pass
        async with Transactions.atomic():
            await Tournament.objects.count()
        assert isinstance(outer, TransactionClient)
        assert outer._pending_savepoints == []
