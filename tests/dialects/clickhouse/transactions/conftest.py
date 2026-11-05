"""Transaction tests require a configured ClickHouse server with Keeper and experimental transactions enabled."""

import contextlib
import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from tests.dialects.clickhouse.constants import CLICKHOUSE_KEEPER_HOSTS
from tests.utils.database_under_test import DatabaseUnderTest


@contextlib.asynccontextmanager
async def open_transactions_context(url_options):
    """Open the transaction models; setup failures fail the selected profile."""
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.transactions.models"],
        db_url=DatabaseUnderTest.get_url_with_options(url_options, db_url),
        app_label="models",
        connection_label="models",
    ) as context:
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
    keeper_options = (
        "" if "keeper_hosts=" in DatabaseUnderTest.get_url() else f"&keeper_hosts={CLICKHOUSE_KEEPER_HOSTS}"
    )
    async with open_transactions_context(f"transactions=true{keeper_options}") as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_keeper_transactions_db(clickhouse_keeper_transactions_module):
    yield clickhouse_keeper_transactions_module
    await truncate_all_models()
