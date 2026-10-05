import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.pool_connections import expire_pool_connections


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_reconnect(db_isolated):
    """Test reconnection after connection expiry."""
    await Tournament.objects.create(name="1")

    await expire_pool_connections(Connections.get("models"))

    await Tournament.objects.create(name="2")

    await expire_pool_connections(Connections.get("models"))

    await Tournament.objects.create(name="3")

    assert [f"{a.id}:{a.name}" for a in await Tournament.objects.all()] == ["1:1", "2:2", "3:3"]


@requires_features(dialect="postgresql", supports_transactions=True)
@pytest.mark.asyncio
async def test_reconnect_transaction_start(db_isolated):
    """Test reconnection at transaction start."""
    async with Transactions.atomic():
        await Tournament.objects.create(name="1")

    await expire_pool_connections(Connections.get("models"))

    async with Transactions.atomic():
        await Tournament.objects.create(name="2")

    await expire_pool_connections(Connections.get("models"))

    async with Transactions.atomic():
        assert [f"{a.id}:{a.name}" for a in await Tournament.objects.all()] == ["1:1", "2:2"]
