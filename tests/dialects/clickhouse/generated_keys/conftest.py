"""The ClickHouse generated keys tests run on the ClickHouse test server - a server without key series
(older than 25.1, or without a ClickHouse Keeper) refuses their models when they are bound, and the tests
are skipped there."""

import contextlib
import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from hare.exceptions import ConfigurationError


@pytest_asyncio.fixture(scope="module")
async def clickhouse_generated_keys_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with contextlib.AsyncExitStack() as stack:
        try:
            context = await stack.enter_async_context(
                hare_test_context(
                    modules=["tests.dialects.clickhouse.generated_keys.models"],
                    db_url=db_url,
                    app_label="models",
                    connection_label="models",
                )
            )
        except ConfigurationError as error:
            pytest.skip(f"the server takes no keys from a series: {error}")
        yield context


@pytest_asyncio.fixture
async def clickhouse_generated_keys_db(clickhouse_generated_keys_module):
    yield clickhouse_generated_keys_module
    await truncate_all_models()
