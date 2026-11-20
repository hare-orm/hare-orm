"""Local column factories retain exact conversions without changing external clients."""

import datetime
import io
from types import SimpleNamespace

import pytest
from clickhouse_driver.columns import service
from clickhouse_driver.columns.datetimecolumn import DateTime64Column
from clickhouse_driver.columns.tuplecolumn import TupleColumn

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_column_types import (
    ClickhouseDriverColumnTypes,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
    ClickhouseDriverLibraryClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_connection import (
    ClickhouseDriverLibraryConnection,
)

pytestmark = pytest.mark.database_independent


def test_nested_columns_are_isolated_and_keep_exact_moments_and_decimal_scale():
    originals = (service.get_column_by_spec, DateTime64Column.after_read_items, TupleColumn.write_data)
    context = SimpleNamespace(
        client_settings={"use_numpy": False, "strings_encoding": "utf-8", "strings_as_bytes": False},
        settings={},
        server_info=SimpleNamespace(get_timezone=lambda: "UTC"),
    )
    column = ClickhouseDriverColumnTypes.get_column_by_specification(
        "Tuple(DateTime64(6, 'UTC'), Decimal(18, 2))", {"context": context}
    )
    moment, decimal = column.nested_columns
    assert moment.after_read_items((-500000,)) == (datetime.datetime(1969, 12, 31, 23, 59, 59, 500000, datetime.UTC),)
    assert str(decimal.after_read_items((125,))[0]) == "1.25"
    empty_arrays = ClickhouseDriverColumnTypes.get_column_by_specification(
        "Array(Tuple(Int32, String))", {"context": context}
    )
    output = io.BytesIO()
    empty_arrays.write_data([[], []], output)
    assert output.getvalue() == bytes(16)
    client = ClickhouseDriverLibraryClient(host="db.local")
    assert isinstance(client.connection, ClickhouseDriverLibraryConnection)
    assert not client.connection.connected
    assert originals == (service.get_column_by_spec, DateTime64Column.after_read_items, TupleColumn.write_data)
