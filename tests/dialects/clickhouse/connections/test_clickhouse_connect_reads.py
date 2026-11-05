"""HTTP reads retain values, metadata and server errors through the local Transform boundary."""

import datetime
import os

import pytest
from clickhouse_connect.driver.external import ExternalData

from hare import Connections
from hare.exceptions import OperationalError


@pytest.fixture
def connect_db(clickhouse_db):
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse+clickhouse-connect"):
        pytest.skip("runs on the clickhouse-connect driver")
    return clickhouse_db


@pytest.mark.asyncio
async def test_native_response_preserves_values_and_column_names(connect_db):
    client = Connections.get("models")
    sql = (
        "SELECT number, toString(number) AS text, toDateTime64('2024-01-02 03:04:05.123456', 6, 'UTC') AS moment, "
        "[number, number + 1] AS pair, NULL AS nothing FROM numbers(5)"
    )
    result = await client.execute_described(sql)
    assert result.columns == ("number", "text", "moment", "pair", "nothing")
    for index, row in enumerate(result.rows):
        assert row[0:2] == (index, str(index))
        assert row[2].replace(tzinfo=None) == datetime.datetime(2024, 1, 2, 3, 4, 5, 123456)
        assert list(row[3]) == [index, index + 1]
        assert row[4] is None
    assert len(result.rows) == 5


@pytest.mark.asyncio
async def test_multiple_response_blocks_preserve_every_row(connect_db):
    client = Connections.get("models")
    result = await client.execute(
        "SELECT number, repeat('x', 20) AS padding FROM numbers(2048) SETTINGS max_block_size=128",
        rows_by_position=True,
    )
    assert result.rows == [(index, "x" * 20) for index in range(2048)]


@pytest.mark.asyncio
async def test_external_data_uses_the_standard_transport(connect_db):
    client = Connections.get("models")
    await client.open_connection()
    external_data = ExternalData(file_name="values", data=b"1\n2\n3\n", fmt="TabSeparated", structure="value UInt8")
    result = await client._connection.query("SELECT sum(value) FROM values", external_data=external_data)
    assert result.result_rows == [(6,)]


@pytest.mark.asyncio
async def test_a_server_error_keeps_the_connection_usable(connect_db):
    client = Connections.get("models")
    with pytest.raises(OperationalError):
        await client.execute("SELECT no_such_column FROM numbers(1)")
    assert (await client.execute("SELECT 1", rows_by_position=True)).rows == [(1,)]
