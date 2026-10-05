"""The ClickHouse transaction tests run on the ClickHouse test server, its connection asking for
transactions (``transactions=true``) - the server is its own Keeper with transactions on; the refusal
tests name it in ``keeper_hosts`` too. A server running no transactions refuses the connection, and the
tests are skipped there."""

import contextlib
import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from hare.exceptions import ConfigurationError
from tests.dialects.clickhouse.constants import CLICKHOUSE_KEEPER_HOSTS


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
async def clickhouse_keeper_transactions_module():
    async with open_transactions_context(f"transactions=true&keeper_hosts={CLICKHOUSE_KEEPER_HOSTS}") as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_keeper_transactions_db(clickhouse_keeper_transactions_module):
    yield clickhouse_keeper_transactions_module
    await truncate_all_models()
