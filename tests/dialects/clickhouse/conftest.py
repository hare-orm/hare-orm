"""The ClickHouse dialect's own fixtures - its tests run against the ClickHouse test server
(``make test_clickhouse_up`` and ``make test_clickhouse``) and are skipped on any other database."""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models


@pytest_asyncio.fixture(scope="module")
async def clickhouse_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.models"], db_url=db_url, app_label="models", connection_label="models"
    ) as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_db(clickhouse_module):
    yield clickhouse_module
    await truncate_all_models()
