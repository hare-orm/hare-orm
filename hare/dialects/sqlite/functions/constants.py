from __future__ import annotations

import datetime
from collections.abc import Callable

#: A sanity ceiling, not a protection against catastrophic backtracking - a short pattern can hang
#: re.search() too.
MAX_REGEX_PATTERN_LENGTH = 1000

#: The beginning of every error of a regular expression - Python's ``re`` message follows it.
REGEX_ERROR_PREFIX = "invalid regular expression: "

#: Size of the compiled-pattern cache of the SQLite ``REGEXP``/``MATCH`` functions.
REGEX_PATTERN_CACHE_SIZE = 256

#: Length of an ISO date text (``YYYY-MM-DD``) - a stored date, not a datetime or a time.
DATE_TEXT_LENGTH = 10

#: Each EXTRACT() date part to the function the hare_extract_date_part UDF applies to a
#: zone-adjusted datetime - SQLite has no EXTRACT().
DATE_PART_EXTRACTORS: dict[str, Callable[[datetime.datetime], int]] = {
    "YEAR": lambda value: value.year,
    "ISOYEAR": lambda value: value.isocalendar()[0],
    "QUARTER": lambda value: (value.month - 1) // 3 + 1,
    "MONTH": lambda value: value.month,
    "WEEK": lambda value: value.isocalendar()[1],
    # 1 (Sunday) to 7 (Saturday), hare's week_day.
    "DOW": lambda value: value.isoweekday() % 7 + 1,
    "ISODOW": lambda value: value.isoweekday(),
    "DAY": lambda value: value.day,
    "HOUR": lambda value: value.hour,
    "MINUTE": lambda value: value.minute,
    "SECOND": lambda value: value.second,
    "MICROSECOND": lambda value: value.microsecond,
}

#: The start of Unix time, the zero of the instant a JSON ``__filter`` aware datetime comparison uses.
SQLITE_JSON_DATETIME_EPOCH = datetime.datetime(1970, 1, 1)

#: The most bytes SQLite holds in one value (``SQLITE_MAX_LENGTH``), and its error of a longer one.
SQLITE_MAX_VALUE_BYTES = 1_000_000_000
SQLITE_TOO_BIG_MESSAGE = "string or blob too big"

#: The bytes of UTF-8's longest character - what a padded text may take per character.
UTF8_LONGEST_CHARACTER_BYTES = 4

#: The functions of ``TemporalArithmeticFunctions`` a connection calls.
TEMPORAL_ARITHMETIC_FUNCTION_NAMES = ("shift_datetime", "shift_date", "difference_datetime", "difference_date")

#: The error of a moment shifted beyond the moments a field holds.
TIMESTAMP_OUT_OF_RANGE_MESSAGE = "timestamp out of range"

#: The error of a date shifted beyond the dates a field holds.
DATE_OUT_OF_RANGE_MESSAGE = "date out of range"
