from __future__ import annotations

import datetime

#: The ranges of ClickHouse's integer types.
INT8_RANGE = (-(2**7), 2**7 - 1)
UINT8_RANGE = (0, 2**8 - 1)
UINT16_RANGE = (0, 2**16 - 1)
UINT32_RANGE = (0, 2**32 - 1)
UINT64_RANGE = (0, 2**64 - 1)
UINT128_RANGE = (0, 2**128 - 1)
UINT256_RANGE = (0, 2**256 - 1)
INT128_RANGE = (-(2**127), 2**127 - 1)
INT256_RANGE = (-(2**255), 2**255 - 1)
#: The largest finite float32.
FLOAT32_MAX = 3.4028234663852886e38
#: The longest FixedString - ClickHouse's own bound is far above any sensible fixed width.
FIXED_STRING_MAX_LENGTH = 65535
#: The byte a FixedString pads its value with.
FIXED_STRING_PAD_BYTE = b"\x00"
#: The first and the last day a ``Date`` column holds - two bytes of days since 1970.
CLICKHOUSE_DATE_RANGE = (datetime.date(1970, 1, 1), datetime.date(2149, 6, 6))
#: The first and the last moment a ``DateTime`` column holds - four bytes of seconds since 1970.
CLICKHOUSE_DATETIME_RANGE = (
    datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC),
    datetime.datetime(2106, 2, 7, 6, 28, 15, tzinfo=datetime.UTC),
)
#: The first and the last moment a ``DateTime64`` column holds.
CLICKHOUSE_DATETIME64_RANGE = (
    datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC),
    datetime.datetime(2299, 12, 31, 23, 59, 59, 999999, tzinfo=datetime.UTC),
)
#: The first and the last day a ``Date32`` column holds.
CLICKHOUSE_DATE32_RANGE = (datetime.date(1900, 1, 1), datetime.date(2299, 12, 31))
#: The last moment a ``DateTime64(9)`` holds - its ticks are a 64-bit count of nanoseconds from 1970.
CLICKHOUSE_DATETIME64_NANOSECONDS_LAST_MOMENT = datetime.datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=datetime.UTC)
#: The most digits after the second a ``DateTime64`` keeps.
CLICKHOUSE_DATETIME64_MAX_PRECISION = 9
#: The digits after the second a Python datetime keeps.
PYTHON_DATETIME_PRECISION = 6
