"""The ClickHouse cluster tests run against the two servers of the test cluster
(``make test_clickhouse_cluster_up`` and ``make test_clickhouse_cluster``) - HARE_TEST_CLICKHOUSE_CLUSTER_DB
names the first of them with its cluster; without it the tests are skipped."""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, truncate_all_models


@pytest_asyncio.fixture(scope="module")
async def clickhouse_cluster_module():
    db_url = os.getenv("HARE_TEST_CLICKHOUSE_CLUSTER_DB", "")
    if not db_url:
        pytest.skip("runs against the ClickHouse test cluster - make test_clickhouse_cluster")
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.cluster.models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def clickhouse_cluster_db(clickhouse_cluster_module):
    yield clickhouse_cluster_module
    await truncate_all_models()
