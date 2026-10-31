from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

from hare.fields.uuid7 import Uuid7
from hare.query.enums import Lookup
from hare.sql.types.sql_types import SqlTypes

#: A date/datetime string written to a DateField/DatetimeField must start with a full calendar
#: date (``2024-05-01`` or ``20240501``) - ISO 8601's reduced forms (``2024``, ``2024-05``) name a
#: period, not a day, and parsers disagree on whether they're accepted at all.
ISO_FULL_DATE_PREFIX_PATTERN = re.compile(r"\s*\d{4}(?:-\d{2}-\d{2}|\d{4})(?!\d)")

#: The lookups of a BinaryField - equality, membership and byte order; a text lookup would match the
#: text a database writes of the bytes, not the bytes.
BINARY_FIELD_SUPPORTED_LOOKUPS = frozenset(
    {
        Lookup.EXACT,
        Lookup.NOT,
        Lookup.GT,
        Lookup.GTE,
        Lookup.LT,
        Lookup.LTE,
        Lookup.IN,
        Lookup.NOT_IN,
        Lookup.RANGE,
        Lookup.ISNULL,
        Lookup.NOT_ISNULL,
    }
)

#: The type of a JSON value as a function's argument - text, number, boolean or null as stored, read
#: by no function of text or numbers alike on every database: it is cast to the type meant first.
JSON_VALUE_TYPE = "a JSON value"

#: The types of values of the fields no other field's values are stored in, as refusals name them - an
#: UPDATE setting such a field to another type's value, or another field to one, is refused.
BINARY_VALUE_TYPE = "bytes"
IP_ADDRESS_VALUE_TYPE = "an IP address"
ARRAY_VALUE_TYPE = "an array"
MAP_VALUE_TYPE = "a map"
TUPLE_VALUE_TYPE = "a tuple"
VECTOR_VALUE_TYPE = "a vector"
GEOMETRY_VALUE_TYPE = "a geometry"
EXTENT_VALUE_TYPE = "an extent"

#: The digits a float32 is written with at most - its shortest text that reads back as the same
#: float32 is searched from FLOAT32_MIN_DIGITS up.
FLOAT32_MIN_DIGITS = 6
FLOAT32_MAX_DIGITS = 9

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
#: the ORJSON_INTEGER_MIN..ORJSON_INTEGER_MAX range, a NaN/infinite float, a
#: list or dict holding itself, containers nested deeper than JSON_MAXIMUM_NESTING (0 - storable as
#: it is - has no name of its own).
JSON_LONG_INTEGER_PROBLEM = 1

JSON_NON_FINITE_FLOAT_PROBLEM = 3

JSON_CIRCULAR_REFERENCE_PROBLEM = 4

JSON_NESTED_TOO_DEEPLY_PROBLEM = 5

#: The most lists, tuples and dicts a JSONField value nests inside each other - orjson's limit for
#: lists and dicts; a deeper tuple overflows its stack and ends the process.
JSON_MAXIMUM_NESTING = 254

#: The count of containers JSONField.check_storable_value() walks in Python before it looks for a
#: container holding itself - such a value never ends the walk, so it gets there; a smaller one pays
#: one count per container.
JSON_CIRCULAR_REFERENCE_CHECK_CONTAINERS = 10_000

#: The message of a JSON value holding itself - it has no end to write.
JSON_CIRCULAR_REFERENCE_MESSAGE = "the value holds itself (a circular reference), which JSON can't store"

#: The message of a JSON value nesting more than JSON_MAXIMUM_NESTING containers.
JSON_NESTED_TOO_DEEPLY_MESSAGE = (
    f"the value nests more than {JSON_MAXIMUM_NESTING} lists, tuples and dicts inside each other, "
    "which JSON can't store"
)

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


#: The hour a time text starts with at the end of a day (24:00) - no time of day holds it.
END_OF_DAY_HOUR_TEXT = "24"

#: The default of a UUIDField primary key by the field's version - none for another version.
UUID_VERSION_DEFAULTS: dict[int | None, Callable[[], UUID]] = {None: uuid4, 4: uuid4, 7: Uuid7.new}
