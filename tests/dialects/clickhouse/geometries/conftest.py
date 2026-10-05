"""The ClickHouse geometry tests run on the ClickHouse test server - a server without the line geometry types refuses
their tables, and the tests are skipped there."""

import contextlib
import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from hare.exceptions import UnSupportedError


@pytest_asyncio.fixture(scope="module")
async def clickhouse_geometries_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with contextlib.AsyncExitStack() as stack:
        try:
            context = await stack.enter_async_context(
                hare_test_context(
                    modules=["tests.dialects.clickhouse.geometries.models"],
                    db_url=db_url,
                    app_label="models",
                    connection_label="models",
                )
            )
        except UnSupportedError as error:
            pytest.skip(f"the server lacks the line geometry types: {error}")
        yield context


@pytest_asyncio.fixture
async def clickhouse_geometries_db(clickhouse_geometries_module):
    yield clickhouse_geometries_module
    await truncate_all_models()
