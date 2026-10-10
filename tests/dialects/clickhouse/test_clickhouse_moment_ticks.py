"""The moments of a ClickHouse binary insert are written as the whole ticks of their column - by the
native module where it is built, exactly as the Python conversion (clickhouse-driver) and the
library's own conversion (clickhouse-connect) write them."""

import datetime
import zoneinfo

import pytest

from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_MOMENT_EPOCH
from hare.native.native_modules import NativeModules


class MomentSubclass(datetime.datetime):
    pass


BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
MOMENTS = [
    CLICKHOUSE_MOMENT_EPOCH,
    datetime.datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=datetime.UTC),
    datetime.datetime(1969, 12, 31, 23, 59, 59, 1, tzinfo=datetime.UTC),
    datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC),
    datetime.datetime(2299, 12, 31, 23, 59, 59, 999_999, tzinfo=datetime.UTC),
    datetime.datetime(2024, 3, 31, 2, 30, 0, 123_456, tzinfo=BERLIN),
    datetime.datetime(2024, 10, 27, 2, 30, fold=1, tzinfo=BERLIN),
    datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5, minutes=-30))),
    MomentSubclass(2001, 2, 3, 4, 5, 6, 7, tzinfo=datetime.UTC),
]


@pytest.fixture
def native_rows():
    if NativeModules.rows is None:
        pytest.skip("the native module isn't built")
    return NativeModules.rows


@pytest.mark.parametrize("moment_scale", range(10))
def test_clickhouse_driver_ticks_are_the_python_conversion_s(native_rows, monkeypatch, moment_scale):
    pytest.importorskip("clickhouse_driver")
    from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
        ClickhouseDriverLibraryClient,
    )

    # None and ticks already are kept as they are.
    values = [*MOMENTS, None, 1_700_000_000_123_456]
    monkeypatch.setattr(ClickhouseDriverLibraryClient, "native_rows", None)
    expected = ClickhouseDriverLibraryClient._get_moment_ticks(values, moment_scale)
    monkeypatch.setattr(ClickhouseDriverLibraryClient, "native_rows", native_rows)
    ticks = ClickhouseDriverLibraryClient._get_moment_ticks(values, moment_scale)
    assert ticks == expected
    assert [type(tick) for tick in ticks] == [type(tick) for tick in expected]
    assert ClickhouseDriverLibraryClient._get_moment_ticks(tuple(values), moment_scale) == expected


def test_a_naive_moment_fails_as_in_the_python_conversion(native_rows, monkeypatch):
    pytest.importorskip("clickhouse_driver")
    from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
        ClickhouseDriverLibraryClient,
    )

    messages = []
    for rows in (None, native_rows):
        monkeypatch.setattr(ClickhouseDriverLibraryClient, "native_rows", rows)
        with pytest.raises(TypeError) as error:
            ClickhouseDriverLibraryClient._get_moment_ticks([datetime.datetime(2020, 1, 1)], 6)
        messages.append(str(error.value))
    assert messages[0] == messages[1]


@pytest.mark.parametrize("moment_scale", range(10))
def test_clickhouse_connect_ticks_are_the_library_s(native_rows, moment_scale):
    pytest.importorskip("clickhouse_connect")
    from clickhouse_connect.datatypes.registry import get_from_name

    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    moments = [moment for moment in MOMENTS if type(moment) is datetime.datetime]
    column_type = get_from_name(f"Nullable(DateTime64({moment_scale}, 'UTC'))")
    columns = ClickhouseConnectClient._get_moment_tick_columns([[*moments, None]], [column_type])
    assert columns[0] == [*(column_type._datetime64_ticks(moment) for moment in moments), None]


def test_clickhouse_connect_leaves_other_columns_to_the_library(native_rows):
    pytest.importorskip("clickhouse_connect")
    from clickhouse_connect.datatypes.registry import get_from_name

    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    # A moment of a datetime subclass, a naive moment and a text are the library's to convert.
    columns = [MOMENTS, MOMENTS, MOMENTS, [datetime.datetime(2020, 1, 1)], ["2020-01-01 00:00:00"]]
    column_types = [
        get_from_name("DateTime('UTC')"),
        get_from_name("Dynamic"),
        get_from_name("DateTime64(6)"),
        get_from_name("DateTime64(6)"),
        get_from_name("DateTime64(6)"),
    ]
    tick_columns = ClickhouseConnectClient._get_moment_tick_columns(columns, column_types)
    assert all(tick_column is column for tick_column, column in zip(tick_columns, columns, strict=True))
