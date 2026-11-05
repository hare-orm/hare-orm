"""Generated-key tests require a configured ClickHouse server with a working Keeper."""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models


@pytest_asyncio.fixture(scope="module")
async def clickhouse_generated_keys_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.generated_keys.models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_generated_keys_db(clickhouse_generated_keys_module):
    yield clickhouse_generated_keys_module
    await truncate_all_models()
