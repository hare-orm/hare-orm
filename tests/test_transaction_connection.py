"""acquire_connection() of a transaction - the connection the transaction runs on, on every driver: a
block under it runs inside the transaction (a savepoint still pending is opened first), and the
transaction still commits or rolls back as a whole."""

import pytest

from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


class RolledBack(Exception):
    pass


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_transaction_gives_the_connection_it_runs_on(db_truncate):
    with pytest.raises(RolledBack):
        async with Transactions.atomic("models") as outer:
            async with outer.acquire_connection() as outer_connection:
                assert outer_connection is not None
            await Tournament.objects.using(outer).create(name="outer")
            async with Transactions.atomic("models") as inner:
                async with inner.acquire_connection() as inner_connection:
                    assert inner_connection is not None
                await Tournament.objects.using(inner).create(name="inner")
            assert await Tournament.objects.using(outer).count() == 2
            raise RolledBack
    assert await Tournament.objects.count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_savepoint_rolled_back_after_its_connection_was_taken_keeps_the_outer_rows(db_truncate):
    async with Transactions.atomic("models") as outer:
        await Tournament.objects.using(outer).create(name="kept")
        with pytest.raises(RolledBack):
            async with Transactions.atomic("models") as inner:
                async with inner.acquire_connection():
                    pass
                await Tournament.objects.using(inner).create(name="dropped")
                raise RolledBack
    assert await Tournament.objects.values_list("name", flat=True) == ["kept"]
