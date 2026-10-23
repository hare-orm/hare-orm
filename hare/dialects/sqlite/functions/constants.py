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

#: The texts ``Cast()`` to a boolean takes on SQLite, lowercase, and the number each is.
SQLITE_CAST_BOOLEAN_TEXTS = {"true": 1, "false": 0, "1": 1, "0": 0}

#: Name of the Python UDF registered on every SQLite connection that gives any stored value's
#: exact decimal text (a double at its shortest round-tripping digits), compared under
#: `SQLITE_DECIMAL_COLLATION_NAME`.
SQLITE_DECIMAL_TEXT_FUNCTION_NAME = "hare_decimal_text"

#: Name of the Python UDF registered on every SQLite connection that gives the text a
#: `DECIMAL(max_digits, decimal_places)` column stores for a value an UPDATE sets it to.
SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME = "hare_decimal_stored_text"

#: The UDFs computing a sum, difference, product and quotient of Decimals exactly - SQLite's own
#: arithmetic runs on doubles. By the arithmetic operator's name.
SQLITE_DECIMAL_OPERATION_FUNCTION_NAMES = {
    "add": "hare_decimal_add",
    "sub": "hare_decimal_subtract",
    "mul": "hare_decimal_multiply",
    "truediv": "hare_decimal_divide",
}

#: The SQL operator of each Decimal operation in SQL kept in DDL, which names no function.
SQLITE_DECIMAL_OPERATION_SQL_OPERATORS = {"add": "+", "sub": "-", "mul": "*", "truediv": "/"}

#: The UDF of an exact remainder of Decimals.
SQLITE_DECIMAL_MOD_FUNCTION_NAME = "hare_decimal_mod"

#: The aggregates - window functions too - summing and averaging Decimals exactly.
SQLITE_DECIMAL_SUM_FUNCTION_NAME = "hare_decimal_sum"
SQLITE_DECIMAL_AVERAGE_FUNCTION_NAME = "hare_decimal_avg"

#: The decimal aggregate taking the place of SQLite's own, by the SQL function's name.
SQLITE_DECIMAL_AGGREGATE_FUNCTION_NAMES = {
    "SUM": SQLITE_DECIMAL_SUM_FUNCTION_NAME,
    "AVG": SQLITE_DECIMAL_AVERAGE_FUNCTION_NAME,
}

#: The most bytes SQLite holds in one value (``SQLITE_MAX_LENGTH``), and its error of a longer one.
SQLITE_MAX_VALUE_BYTES = 1_000_000_000
SQLITE_TOO_BIG_MESSAGE = "string or blob too big"

#: The bytes of UTF-8's longest character - what a padded text may take per character.
UTF8_LONGEST_CHARACTER_BYTES = 4

#: The error of an integer beyond its type.
INTEGER_OUT_OF_RANGE_MESSAGE = "integer out of range"

#: The functions of ``TemporalArithmeticFunctions`` a connection calls.
TEMPORAL_ARITHMETIC_FUNCTION_NAMES = ("shift_datetime", "shift_date", "difference_datetime", "difference_date")

#: The error of a moment shifted beyond the moments a field holds.
TIMESTAMP_OUT_OF_RANGE_MESSAGE = "timestamp out of range"

#: The error of a date shifted beyond the dates a field holds.
DATE_OUT_OF_RANGE_MESSAGE = "date out of range"

#: The first byte of the sort key of an INTEGER or REAL, a TEXT and a BLOB value - SQLite orders
#: values of different storage classes so.
SQLITE_SORT_KEY_NUMBER_CLASS = b"\x10"

SQLITE_SORT_KEY_TEXT_CLASS = b"\x20"

SQLITE_SORT_KEY_BLOB_CLASS = b"\x30"
