"""The native date part extraction and date truncation give what the Python functions give, in UTC,
in zones with daylight saving time changes and for naive values - every part and every unit."""

from zoneinfo import ZoneInfo

import pytest

from hare.dialects.sqlite.functions.constants import DATE_PART_EXTRACTORS
from hare.dialects.sqlite.functions.datetime.date_truncation import DateTruncation
from hare.dialects.sqlite.functions.datetime.sqlite_date_part_extraction import SqliteDatePartExtraction
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.sql.enums import TruncType

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

VALUES = [
    "2024-03-10 06:59:59+00:00", "2024-03-10 07:00:00+00:00", "2024-11-03 05:30:00+00:00",
    "2024-11-03 06:30:00.250000+00:00", "2024-12-31 23:59:59.999999+00:00", "2024-01-01 00:00:00+00:00",
    "2023-01-01 12:00:00+05:30", "2024-02-29 10:15:30-03:00", "2024-02-29 10:15:30", "2024-02-29T10:15",
    "0001-01-01 00:00:00", "0001-01-01 00:00:00+00:00", "9999-12-31 23:59:59+00:00", "2024-02-29",
    "2021-01-03", "10:15:30", "10:15:30+02:00", "2024-02-29 10:15:30Z", "2024-02-29 10:15:30.1234567+00:00",
    "not a date", None,
]  # fmt: skip
ZONES = [None, "", "UTC", "America/New_York", "Europe/Moscow", "Asia/Kolkata", "Australia/Lord_Howe"]


@pytest.fixture
def native():
    return SqliteNativeFunctions.module.DateFunctions(
        ZoneInfo, {}, SqliteDatePartExtraction.extract, DateTruncation.truncate
    )


def call_both(native_call, python_call, *arguments):
    try:
        expected = python_call(*arguments)
    except Exception as error:  # noqa: BLE001 - the error is what is compared.
        with pytest.raises(type(error)):
            native_call(*arguments)
        return
    assert native_call(*arguments) == expected, arguments


def test_extract(native) -> None:
    for part in [*DATE_PART_EXTRACTORS, "DATE", "TIME", "CENTURY"]:
        for value in VALUES:
            for zone_name in ZONES:
                call_both(native.extract, SqliteDatePartExtraction.extract, part, value, zone_name)


def test_truncate(native) -> None:
    for unit in [*TruncType, "decade"]:
        for value in VALUES:
            for zone_name in ZONES:
                call_both(native.truncate, DateTruncation.truncate, str(unit), value, zone_name)
