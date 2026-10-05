"""The ClickHouse transaction tests run on the ClickHouse test server, its connection asking for
transactions (``transactions=true``) - the server is its own Keeper with transactions on; the row lock
tests name it as the Keeper of the locks too. A server running no transactions refuses the connection,
and the tests are skipped there."""

import contextlib
import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from hare.exceptions import ConfigurationError


@contextlib.asynccontextmanager
async def open_transactions_context(url_options):
    """The test context of the transaction models on a connection with the given URL options - the tests
    skipped on a server refusing it."""
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with contextlib.AsyncExitStack() as stack:
        try:
            context = await stack.enter_async_context(
                hare_test_context(
                    modules=["tests.dialects.clickhouse.transactions.models"],
                    db_url=f"{db_url}?{url_options}",
                    app_label="models",
                    connection_label="models",
                )
            )
        except ConfigurationError as error:
            pytest.skip(f"the server runs no transactions: {error}")
        yield context


@pytest_asyncio.fixture(scope="module")
async def clickhouse_transactions_module():
    async with open_transactions_context("transactions=true") as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_transactions_db(clickhouse_transactions_module):
    yield clickhouse_transactions_module
    await truncate_all_models()


@pytest_asyncio.fixture(scope="module")
async def clickhouse_row_locks_module():
    # The Keeper of the test server listens on 9182 of the host.
    async with open_transactions_context("transactions=true&keeper_hosts=127.0.0.1:9182") as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_row_locks_db(clickhouse_row_locks_module):
    yield clickhouse_row_locks_module
    await truncate_all_models()
