"""The ClickHouse dialect's own fixtures - its tests run against the ClickHouse test server
(``make test_clickhouse_up`` and ``make test_clickhouse``) and are skipped on any other database."""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.fixture
def skip_without_clickhouse():
    DatabaseUnderTest.skip_unless_clickhouse()


@pytest_asyncio.fixture(scope="module")
async def clickhouse_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    DatabaseUnderTest.skip_unless_clickhouse(db_url)
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.models"], db_url=db_url, app_label="models", connection_label="models"
    ) as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_db(clickhouse_module):
    yield clickhouse_module
    await truncate_all_models()


@pytest.fixture
def connect_db(clickhouse_db):
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse+clickhouse-connect"):
        pytest.skip("runs on the clickhouse-connect driver")
    return clickhouse_db
