"""The clickhouse-connect driver reads a response whole and parses it on the event loop - the rows
are the ones the library's own query() gives, a large response parsed on a worker thread."""

import datetime
import os

import pytest

pytest.importorskip("clickhouse_connect")

from clickhouse_connect.driver.external import ExternalData  # noqa: E402

from hare import Connections  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_whole_response_query import (  # noqa: E402
    ClickhouseConnectWholeResponseQuery,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.constants import (  # noqa: E402
    CLICKHOUSE_CONNECT_INLINE_PARSE_MAX_BYTES,
)


@pytest.fixture
def connect_db(clickhouse_db):
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse+clickhouse-connect"):
        pytest.skip("runs on the clickhouse-connect driver")
    return clickhouse_db


async def get_library_client():
    connection = Connections.get("models")
    await connection.open_connection()
    return connection._connection


@pytest.mark.asyncio
async def test_the_library_parts_are_found():
    assert ClickhouseConnectWholeResponseQuery.get_library_functions()


@pytest.mark.asyncio
async def test_a_small_response_gives_the_library_s_rows(connect_db):
    library_client = await get_library_client()
    sql = (
        "SELECT number, toString(number) AS text, toDateTime64('2024-01-02 03:04:05.123456', 6, 'UTC') AS moment, "
        "[number, number + 1] AS pair, NULL AS nothing FROM numbers(5)"
    )
    result = await ClickhouseConnectWholeResponseQuery.run(library_client, sql)
    library_result = await library_client.query(sql)
    assert result.result_rows == library_result.result_rows
    assert result.column_names == library_result.column_names
    assert [column_type.name for column_type in result.column_types] == [
        column_type.name for column_type in library_result.column_types
    ]
    assert result.result_rows[0][2].replace(tzinfo=None) == datetime.datetime(2024, 1, 2, 3, 4, 5, 123456)


@pytest.mark.asyncio
async def test_a_large_response_gives_the_library_s_rows(connect_db):
    library_client = await get_library_client()
    row_count = CLICKHOUSE_CONNECT_INLINE_PARSE_MAX_BYTES // 4
    sql = f"SELECT number, repeat('x', 20) AS padding FROM numbers({row_count})"
    result = await ClickhouseConnectWholeResponseQuery.run(library_client, sql)
    assert len(result.result_rows) == row_count
    assert result.result_rows == (await library_client.query(sql)).result_rows


@pytest.mark.asyncio
async def test_external_data_is_sent(connect_db):
    library_client = await get_library_client()
    external_data = ExternalData(file_name="values", data=b"1\n2\n3\n", fmt="TabSeparated", structure="value UInt8")
    result = await ClickhouseConnectWholeResponseQuery.run(
        library_client, "SELECT sum(value) FROM values", external_data
    )
    assert result.result_rows == [(6,)]


@pytest.mark.asyncio
async def test_a_server_error_is_the_library_s(connect_db):
    library_client = await get_library_client()
    with pytest.raises(Exception) as error_info:
        await ClickhouseConnectWholeResponseQuery.run(library_client, "SELECT no_such_column FROM numbers(1)")
    with pytest.raises(type(error_info.value)):
        await library_client.query("SELECT no_such_column FROM numbers(1)")
