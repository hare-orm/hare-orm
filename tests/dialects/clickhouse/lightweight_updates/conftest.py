"""The ClickHouse lightweight update tests run on the ClickHouse test server."""

import contextlib
import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models


@pytest_asyncio.fixture(scope="module")
async def clickhouse_lightweight_updates_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with contextlib.AsyncExitStack() as stack:
        context = await stack.enter_async_context(
            hare_test_context(
                modules=["tests.dialects.clickhouse.lightweight_updates.models"],
                db_url=db_url,
                app_label="models",
                connection_label="models",
            )
        )
        yield context


@pytest_asyncio.fixture
async def clickhouse_lightweight_updates_db(clickhouse_lightweight_updates_module):
    yield clickhouse_lightweight_updates_module
    await truncate_all_models()
