from __future__ import annotations

import re
from decimal import Decimal

from hare.sql.types.sql_types import SqlTypes

#: A date/datetime string written to a DateField/DatetimeField must start with a full calendar
#: date (``2024-05-01`` or ``20240501``) - ISO 8601's reduced forms (``2024``, ``2024-05``) name a
#: period, not a day, and parsers disagree on whether they're accepted at all.
ISO_FULL_DATE_PREFIX_PATTERN = re.compile(r"\s*\d{4}(?:-\d{2}-\d{2}|\d{4})(?!\d)")

#: Value bounds for a signed 16/32/64-bit integer column.
INT16_MIN = -32768

INT16_MAX = 32767

INT32_MIN = -2147483648

INT32_MAX = 2147483647

INT64_MIN = -9223372036854775808

INT64_MAX = 9223372036854775807

#: Truncation length for an auto-generated enum field description.
AUTO_DESCRIPTION_MAX_LENGTH = 2048

#: The SQL type a float/Decimal value compared with an integer column is cast to - a driver would
#: otherwise bind it as an integer.
INT_LOOKUP_LITERAL_CAST_SQL_TYPE = {
    float: SqlTypes.FLOAT,
    Decimal: SqlTypes.NUMERIC,
}

#: The integer range orjson serializes - a JSONField value holding an int outside it is encoded by
#: the standard library instead, which writes any int exactly.
ORJSON_INTEGER_MIN = -(2**63)

ORJSON_INTEGER_MAX = 2**64 - 1

#: bytes.translate() table turning every ASCII digit into b"1" and every other byte into b"0".
JSON_DIGIT_MARKER_TABLE = bytes(ord("1") if ord("0") <= byte <= ord("9") else ord("0") for byte in range(256))

#: The codes rust.native.rows.check_json_storable() returns for a JSONField value: an int outside
#: the ORJSON_INTEGER_MIN..ORJSON_INTEGER_MAX range, a null byte in a str, a NaN/infinite float
#: (0 - storable as it is - has no name of its own).
JSON_LONG_INTEGER_PROBLEM = 1

JSON_NULL_BYTE_PROBLEM = 2

JSON_NON_FINITE_FLOAT_PROBLEM = 3

#: The default length of an EmailField - the longest address a mail path holds (RFC 5321 4.5.3.1.3).
EMAIL_FIELD_MAX_LENGTH = 254

#: The default length of a URLField.
URL_FIELD_MAX_LENGTH = 2048

#: The schemes a URLField accepts by default.
URL_FIELD_SCHEMES = ("http", "https")

#: The default length of a SlugField.
SLUG_FIELD_MAX_LENGTH = 50

#: The length of a PhoneField - an E.164 number: "+" and at most 15 digits.
PHONE_FIELD_MAX_LENGTH = 16

#: A URL scheme a URLField accepts, as it is declared - lowercase.
URL_SCHEME_PATTERN = r"[a-z][a-z0-9+.-]*"
