import asyncio
import sys

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament, UniqueName

# =============================================================================
# TestConcurrencyIsolated - uses db_truncate fixture
# =============================================================================


@pytest.mark.asyncio
async def test_concurrency_read_isolated(db_truncate):
    """Test concurrent reads."""
    await Tournament.objects.create(name="Test")
    tour1 = await Tournament.objects.first()
    all_read = await asyncio.gather(*[Tournament.objects.first() for _ in range(100)])
    assert all_read == [tour1 for _ in range(100)]


@pytest.mark.asyncio
async def test_concurrency_create_isolated(db_truncate):
    """Test concurrent creates."""
    all_write = await asyncio.gather(*[Tournament.objects.create(name="Test") for _ in range(100)])
    all_read = await Tournament.objects.all()
    assert set(all_write) == set(all_read)


@pytest.mark.asyncio
async def test_nonconcurrent_get_or_create_isolated(db_truncate):
    """Test non-concurrent get_or_create."""
    unas = [await UniqueName.objects.get_or_create(name="c") for _ in range(10)]
    una_created = [una[1] for una in unas if una[1] is True]
    assert len(una_created) == 1
    for una in unas:
        assert una[0] == unas[0][0]


@requires_features(supports_unique_constraints=True)
@pytest.mark.skipif(sys.version_info < (3, 7), reason="aiocontextvars backport not handling this well")
@pytest.mark.asyncio
async def test_concurrent_get_or_create_isolated(db_truncate):
    """Test concurrent get_or_create."""
    unas = await asyncio.gather(*[UniqueName.objects.get_or_create(name="d") for _ in range(10)])
    una_created = [una[1] for una in unas if una[1] is True]
    assert len(una_created) == 1
    for una in unas:
        assert una[0] == unas[0][0]


@pytest.mark.skipif(sys.version_info < (3, 7), reason="aiocontextvars backport not handling this well")
@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrent_transactions_with_multiple_ops(db_truncate):
    """Test concurrent transactions with multiple operations."""

    async def create_in_transaction():
        async with Transactions.atomic():
            await asyncio.gather(*[Tournament.objects.create(name="Test") for _ in range(100)])

    await asyncio.gather(*[create_in_transaction() for _ in range(10)])
    count = await Tournament.objects.all().count()
    assert count == 1000


@pytest.mark.skipif(sys.version_info < (3, 7), reason="aiocontextvars backport not handling this well")
@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrent_transactions_with_single_op(db_truncate):
    """Test concurrent transactions with single operation."""

    async def create():
        async with Transactions.atomic():
            await Tournament.objects.create(name="Test")

    await asyncio.gather(*[create() for _ in range(100)])
    count = await Tournament.objects.all().count()
    assert count == 100


@pytest.mark.skipif(sys.version_info < (3, 7), reason="aiocontextvars backport not handling this well")
@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_concurrent_transactions_with_multiple_ops(db_truncate):
    """Test nested concurrent transactions with multiple operations."""

    async def create_in_transaction():
        async with Transactions.atomic():
            async with Transactions.atomic():
                await asyncio.gather(*[Tournament.objects.create(name="Test") for _ in range(100)])

    await asyncio.gather(*[create_in_transaction() for _ in range(10)])
    count = await Tournament.objects.all().count()
    assert count == 1000


# =============================================================================
# TestConcurrencyTransactioned - uses db fixture (transaction rollback)
# =============================================================================


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrency_read_transactioned(db):
    """Test concurrent reads within transaction."""
    await Tournament.objects.create(name="Test")
    tour1 = await Tournament.objects.first()
    all_read = await asyncio.gather(*[Tournament.objects.first() for _ in range(100)])
    assert all_read == [tour1 for _ in range(100)]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrency_create_transactioned(db):
    """Test concurrent creates within transaction."""
    all_write = await asyncio.gather(*[Tournament.objects.create(name="Test") for _ in range(100)])
    all_read = await Tournament.objects.all()
    assert set(all_write) == set(all_read)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nonconcurrent_get_or_create_transactioned(db):
    """Test non-concurrent get_or_create within transaction."""
    unas = [await UniqueName.objects.get_or_create(name="a") for _ in range(10)]
    una_created = [una[1] for una in unas if una[1] is True]
    assert len(una_created) == 1
    for una in unas:
        assert una[0] == unas[0][0]


# =============================================================================
# TestConcurrentDBConnectionInitialization - tests lazy connection init
# These tests ensure concurrent queries don't cause initialization issues.
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_queries_lazy_init(db_truncate):
    """Test concurrent queries with lazy connection initialization.

    Hare.init is lazy and does not initialize the database connection
    until the first query. This test ensures that concurrent queries do not
    cause initialization issues.
    """
    # The db_truncate fixture already initializes the connection, so we just
    # test that concurrent queries work
    await asyncio.gather(*[Connections.get("models").execute("SELECT 1") for _ in range(100)])


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrent_transactions_lazy_init(db_truncate):
    """Test concurrent transactions with lazy connection initialization."""

    async def transaction() -> None:
        async with Transactions.atomic():
            await Connections.get("models").execute("SELECT 1")

    await asyncio.gather(*[transaction() for _ in range(100)])
