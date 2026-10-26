"""The clickhouse-connect driver reports no pool status - its library keeps the HTTP connections."""

import os

import pytest

from hare import Connections
from hare.exceptions import UnSupportedError

REQUIRES = ("clickhouse-connect",)


@pytest.fixture
def connect_db(clickhouse_db):
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse+clickhouse-connect"):
        pytest.skip("runs on the clickhouse-connect driver")
    return clickhouse_db


@pytest.mark.asyncio
async def test_a_client_reporting_no_pool_status_says_so(connect_db):
    client = Connections.current().get_main("models")
    await client.execute_dicts("SELECT 1 AS one")
    with pytest.raises(UnSupportedError, match="reports no pool status"):
        client.get_pool_status()
    assert Connections.get_pool_statuses() == []
