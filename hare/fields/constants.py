import datetime
import re
from decimal import Decimal

from hare.fields.enums import OnDelete
from hare.query.enums import Lookup
from hare.sql.sql_types import SqlTypes

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

#: The naive datetime.max/datetime.min asyncpg decodes a TIMESTAMPTZ 'infinity'/'-infinity' as.
NAIVE_INFINITY_DATETIMES = frozenset({datetime.datetime.max, datetime.datetime.min})

#: Truncation length for an auto-generated enum field description.
AUTO_DESCRIPTION_MAX_LENGTH = 2048

#: Floor for DecimalField's own quantize() Context precision - matches decimal's own default
#: context precision, so a max_digits at or below it keeps the exact previous behavior.
DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION = 28

#: The SQL type a float/Decimal value compared with an integer column is cast to - a driver would
#: otherwise bind it as an integer.
INT_LOOKUP_LITERAL_CAST_SQL_TYPE = {
    float: SqlTypes.FLOAT,
    Decimal: SqlTypes.NUMERIC,
}

#: Shown instead of a sensitive field's value in an error message.
SENSITIVE_VALUE_PLACEHOLDER = "<hidden>"

#: Lookups still usable on an EncryptedTextField - Fernet tokens are non-deterministic (a fresh
#: random IV per write), so no value-comparing lookup could ever match a stored row.
ENCRYPTED_TEXT_FIELD_SUPPORTED_LOOKUPS = frozenset({Lookup.ISNULL, Lookup.NOT_ISNULL})
#: Lookups still usable on an EncryptedJSONField - its dict keys stay plaintext, so key-presence
#: lookups work; every value-comparing lookup can't match for the same reason as above.
ENCRYPTED_JSON_FIELD_SUPPORTED_LOOKUPS = frozenset(
    {Lookup.ISNULL, Lookup.NOT_ISNULL, Lookup.HAS_KEY, Lookup.HAS_KEYS, Lookup.HAS_ANY_KEYS}
)
#: The DDL behind on_delete=PROTECT, which Python enforces before the DELETE: a deferrable NO ACTION
#: - a hard delete defers it for its own DELETE, so a guard the same cascade removes doesn't fail by
#: deletion order. Immediate by default, leaving no pending trigger events.
PROTECT_DB_ON_DELETE_SQL = "NO ACTION DEFERRABLE INITIALLY IMMEDIATE"

#: The integer range orjson serializes - a JSONField value holding an int outside it is encoded by
#: the standard library instead, which writes any int exactly.
ORJSON_INTEGER_MIN = -(2**63)
ORJSON_INTEGER_MAX = 2**64 - 1
#: orjson decodes an integer literal outside its range as a float - a JSON text with a run of at
#: least this many digits is decoded by the standard library instead, which reads any int exactly.
JSON_LONG_INTEGER_MIN_DIGITS = 19
#: bytes.translate() table turning every ASCII digit into b"1" and every other byte into b"0".
JSON_DIGIT_MARKER_TABLE = bytes(ord("1") if ord("0") <= byte <= ord("9") else ord("0") for byte in range(256))
#: A run of JSON_LONG_INTEGER_MIN_DIGITS digits after JSON_DIGIT_MARKER_TABLE is applied.
JSON_LONG_INTEGER_MARKER = b"1" * JSON_LONG_INTEGER_MIN_DIGITS
#: The codes rust.native.rows.check_json_storable() returns for a JSONField value: an int outside
#: the ORJSON_INTEGER_MIN..ORJSON_INTEGER_MAX range, a null byte in a str, a NaN/infinite float
#: (0 - storable as it is - has no name of its own).
JSON_LONG_INTEGER_PROBLEM = 1
JSON_NULL_BYTE_PROBLEM = 2
JSON_NON_FINITE_FLOAT_PROBLEM = 3


class DbDefaultNotSet:
    """Sentinel indicating db_default was not provided."""

    def __repr__(self) -> str:
        return "NOT_PROVIDED"

    def __bool__(self) -> bool:
        return False


DB_DEFAULT_NOT_SET = DbDefaultNotSet()


CASCADE = OnDelete.CASCADE


RESTRICT = OnDelete.RESTRICT


SET_NULL = OnDelete.SET_NULL


SET_DEFAULT = OnDelete.SET_DEFAULT


NO_ACTION = OnDelete.NO_ACTION


PROTECT = OnDelete.PROTECT


UL = "\u00a1-\uffff"


HOSTNAME_REGEX = r"[a-z" + UL + r"0-9](?:[a-z" + UL + r"0-9-]{0,61}[a-z" + UL + r"0-9])?"


# Max length for domain name labels is 63 characters per RFC 1034 sec. 3.1.
DOMAIN_REGEX = r"(?:\.(?!-)[a-z" + UL + r"0-9-]{1,63}(?<!-))*"


# Top-level domain.
TLD_NO_FQDN_REGEX = (
    r"\."  # dot
    r"(?!-)"  # can't start with a dash
    r"(?:[a-z" + UL + "-]{2,63}"  # domain label
    r"|xn--[a-z0-9]{1,59})"  # or punycode label
    r"(?<!-)"  # can't end with a dash
)


TLD_REGEX = TLD_NO_FQDN_REGEX + r"\.?"

#: The rows reencrypt_fields() reads and writes at once, a transaction each.
REENCRYPT_BATCH_SIZE = 500

#: Bind parameters left unused by a many-to-many relation's batched through-table INSERTs, out of the
#: backend's own per-statement ceiling.
M2M_WRITE_BIND_PARAMS_HEADROOM = 100

#: Column-name suffix appended to a relational field's own name to form its DB column.
FK_COLUMN_SUFFIX = "_id"

#: A field not loaded on the instance before the write - unlike None.
ROLLBACK_RESTORE_UNSET = object()

#: Why a text or JSON value holding a null byte is refused - PostgreSQL's text protocol can't carry
#: one in any text type, and SQLite cuts a LIKE pattern at it.
NULL_BYTE_MESSAGE = "value contains a null byte ('\\x00'), which JSON/text columns can't store"
