"""Context shutdown closes the shared client even when a transaction is bound to its alias."""

import os
from contextlib import AsyncExitStack

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import ConfigurationError, DatabaseError, TransactionManagementError
from hare.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", (False, True))
async def test_context_shutdown_rolls_back_and_closes_the_shared_client(tmp_path, monkeypatch, nested):
    if not DatabaseUnderTest.get_dialect().features.supports_transactions:
        pytest.skip("requires transactions")
    url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if url.startswith("sqlite"):
        url = f"sqlite+aiosqlite://{tmp_path / 'shutdown.sqlite3'}"
        monkeypatch.setattr("hare.dialects.sqlite.client.sqlite_client.SQLITE_CLOSE_TIMEOUT_SECONDS", 0)
    # Exercise the pool's forced-close path without waiting for its production grace period.
    if url.startswith("postgresql"):
        driver = "asyncpg" if "+asyncpg" in url else "rust_pg"
        module = f"hare.dialects.postgresql.drivers.{driver}.client.{driver}_client"
        monkeypatch.setattr(f"{module}.POOL_CLOSE_TIMEOUT_SECONDS", 0)
    async with hare_test_context(
        ["tests.shared_models.testmodels"],
        db_url=url,
        connection_label="models",
    ) as context:
        shared = context.get_connection("models")
        config = {
            "connections": dict(context.connections.db_config),
            "apps": {"models": {"models": ["tests.shared_models.testmodels"], "default_connection": "models"}},
        }
        checker = context.connections.create_independent("models")
        closed = False
        try:
            try:
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(Transactions.atomic("models"))
                    if nested:
                        await stack.enter_async_context(Transactions.atomic("models"))
                    await Tournament.objects.create(id=1, name="rolled back")
                    await context.close_connections()
                    closed = True
                    with pytest.raises(ConfigurationError):
                        await Tournament.objects.count()
            except (DatabaseError, TransactionManagementError):
                # A closed connection cannot commit or release its former savepoint.
                pass
            assert closed, "shutdown must close the shared client, not reject the transaction wrapper"
            assert shared.discarded
            _, rows = await checker.execute('SELECT count(*) AS total FROM "tournament"')
            assert rows[0]["total"] == 0
            if url.startswith("postgresql"):
                assert shared._pool is None
        finally:
            await checker.close()
            # Also cleans up the original failing implementation, which forgot its open client.
            await shared.close()
            await context.init(config)
