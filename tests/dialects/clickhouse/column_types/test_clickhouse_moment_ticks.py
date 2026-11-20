"""The moments of a ClickHouse binary insert are written as the whole ticks of their column - by the
native module (clickhouse-driver) exactly as timedelta arithmetic counts them, and exactly as the
library's own conversion (clickhouse-connect) writes them."""

import datetime
import zoneinfo

import pytest

from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_MOMENT_EPOCH

pytestmark = pytest.mark.database_independent


@pytest.mark.requires("clickhouse-connect")
@pytest.mark.parametrize(
    ("type_name", "timezone"),
    [
        ("DateTime", None),
        ("DateTime64(6)", None),
        ("DateTime('Europe/Berlin')", "Europe/Berlin"),
        ("DateTime64(6, 'UTC')", "UTC"),
        ("LowCardinality(Nullable(DateTime64(6, 'Europe/Berlin')))", "Europe/Berlin"),
    ],
)
def test_json_compact_moments_use_the_declared_timezone(type_name, timezone):
    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_json_compact_values import (
        ClickhouseJsonCompactValues,
    )

    text = "2024-01-02 03:04:05.123456"
    expected = datetime.datetime(2024, 1, 2, 3, 4, 5, 123456)
    if timezone is not None:
        expected = expected.replace(tzinfo=zoneinfo.ZoneInfo(timezone))
    assert ClickhouseJsonCompactValues.get_value(type_name, text) == expected
    assert ClickhouseJsonCompactValues.get_value(f"Array({type_name})", [text]) == [expected]
    assert ClickhouseJsonCompactValues.get_value(f"Tuple(`recorded at` {type_name})", {"recorded at": text}) == (
        expected,
    )


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


def get_expected_ticks(moment, moment_scale):
    """The ticks of a moment at a scale, from timedelta arithmetic - finer than a tick rounds down."""
    microseconds = (moment - CLICKHOUSE_MOMENT_EPOCH) // datetime.timedelta(microseconds=1)
    if moment_scale > 6:
        return microseconds * 10 ** (moment_scale - 6)
    return microseconds // 10 ** (6 - moment_scale)


@pytest.mark.requires("clickhouse-driver")
@pytest.mark.parametrize("moment_scale", range(10))
def test_clickhouse_driver_ticks_are_the_whole_ticks_since_the_epoch(moment_scale):
    from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
        ClickhouseDriverLibraryClient,
    )

    # None and ticks already are kept as they are.
    values = [*MOMENTS, None, 1_700_000_000_123_456]
    expected = [*(get_expected_ticks(moment, moment_scale) for moment in MOMENTS), None, 1_700_000_000_123_456]
    ticks = ClickhouseDriverLibraryClient._get_moment_ticks(values, moment_scale)
    assert ticks == expected
    assert [type(tick) for tick in ticks] == [type(tick) for tick in expected]
    assert ClickhouseDriverLibraryClient._get_moment_ticks(tuple(values), moment_scale) == expected


@pytest.mark.requires("clickhouse-driver")
def test_a_naive_moment_fails_as_the_subtraction_from_the_epoch_does():
    from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
        ClickhouseDriverLibraryClient,
    )

    naive_moment = datetime.datetime(2020, 1, 1)
    with pytest.raises(TypeError) as subtraction_error:
        naive_moment - CLICKHOUSE_MOMENT_EPOCH
    with pytest.raises(TypeError) as error:
        ClickhouseDriverLibraryClient._get_moment_ticks([naive_moment], 6)
    assert str(error.value) == str(subtraction_error.value)


@pytest.mark.requires("clickhouse-connect")
@pytest.mark.parametrize("moment_scale", range(10))
def test_clickhouse_connect_ticks_are_the_library_s(moment_scale):
    from clickhouse_connect.datatypes.registry import get_from_name

    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    moments = [moment for moment in MOMENTS if type(moment) is datetime.datetime]
    column_type = get_from_name(f"Nullable(DateTime64({moment_scale}, 'UTC'))")
    columns = ClickhouseConnectClient._get_moment_tick_columns([[*moments, None]], [column_type])
    assert columns[0] == [*(column_type._datetime64_ticks(moment) for moment in moments), None]


@pytest.mark.requires("clickhouse-connect")
def test_clickhouse_connect_leaves_other_columns_to_the_library():
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
