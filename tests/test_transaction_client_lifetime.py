"""A transaction wrapper keeps no reference to itself - one would leave it, and the connection it
holds, alive until a garbage collection instead of freeing it as soon as its block ends."""

import pytest

from hare.dialects.base.client import TransactionClient
from tests.testmodels import Tournament


def refers_to_itself(wrapper: TransactionClient) -> bool:
    return any(value is wrapper for value in vars(wrapper).values())


@pytest.mark.asyncio
async def test_top_level_and_nested_wrappers_keep_no_reference_to_themselves(db):
    connection = Tournament.get_connection()
    if not connection.features.supports_transactions:
        pytest.skip("the database has no transactions")
    async with connection._in_transaction() as transaction:
        top_level = transaction._get_top_level_transaction()
        assert not refers_to_itself(top_level)
        async with transaction._in_transaction() as nested:
            assert nested._get_top_level_transaction() is top_level
            assert not refers_to_itself(nested)
