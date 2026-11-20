"""Geometry tests require the configured ClickHouse profile to support the line geometry types."""

import os

import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def clickhouse_geometries_module():
    db_url = os.getenv("HARE_TEST_DB", "")
    DatabaseUnderTest.skip_unless_clickhouse(db_url)
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.geometries.models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_geometries_db(clickhouse_geometries_module):
    yield clickhouse_geometries_module
    await truncate_all_models()
