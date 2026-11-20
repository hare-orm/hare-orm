"""The clickhouse-connect driver reports no pool status - its library keeps the HTTP connections."""

import pytest

from hare import Connections
from hare.exceptions import UnSupportedError


@pytest.mark.asyncio
async def test_a_client_reporting_no_pool_status_says_so(connect_db):
    client = Connections.current().get_main("models")
    await client.execute_dicts("SELECT 1 AS one")
    with pytest.raises(UnSupportedError, match="reports no pool status"):
        client.get_pool_status()
    assert Connections.get_pool_statuses() == []
