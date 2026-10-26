from __future__ import annotations

from hare.sql.constants import MICROSECONDS_PER_SECOND
from hare.sql.enums import TruncType

#: The units a moment is truncated to by taking its local remainder off the moment itself - the
#: local wall clock of a moment in an hour a DST change repeats is ambiguous, read back as the second
#: of the two moments it names.
POSTGRESQL_REMAINDER_TRUNC_TYPES = frozenset({TruncType.HOUR, TruncType.MINUTE, TruncType.SECOND})

#: The digest functions ENCODE(...(CONVERT_TO(text)), 'hex') gives the hex text of.
POSTGRESQL_DIGEST_FUNCTIONS = frozenset({"SHA224", "SHA256", "SHA384", "SHA512"})

#: The math functions with a NUMERIC variant only - every argument is cast to it.
POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS = frozenset({"MOD", "LOG"})

#: PostgreSQL's Now() db_default of a DateField/TimeField: the current wall clock in {zone} (a
#: quoted zone name or an INTERVAL offset), a TimeField's with {offset} attached.
POSTGRESQL_NOW_DATE_SQL_TEMPLATE = "((STATEMENT_TIMESTAMP() AT TIME ZONE {zone})::date)"

POSTGRESQL_NOW_TIME_SQL_TEMPLATE = (
    "(((((STATEMENT_TIMESTAMP() AT TIME ZONE {zone})::time)::text || '{offset}'))::timetz)"
)

#: PostgreSQL's Now(): the moment of the statement, hare's Now() - CURRENT_TIMESTAMP is the start of
#: the transaction.
POSTGRESQL_NOW_SQL = "STATEMENT_TIMESTAMP()"

POSTGRESQL_NOW_OFFSET_ZONE_TEMPLATE = "INTERVAL '{offset}'"

#: The UTC offset a naive time is bound with (use_timezone=False).
POSTGRESQL_NAIVE_TIME_OFFSET = "+00:00"

#: PostgreSQL's UuidV7() db_default.
POSTGRESQL_UUID_V7_SQL = "uuidv7()"

#: PostgreSQL's RandomHex() db_default: the MD5 of a random number, 32 lower-case hex digits.
POSTGRESQL_RANDOM_HEX_SQL = "md5(random()::text)"

#: A random float from 0 (included) to 1 (excluded).
POSTGRESQL_RANDOM_FLOAT_SQL = "random()"

#: The text of an ``inet`` address without its mask - a cast to text writes ``1.2.3.4/32``.
POSTGRESQL_ADDRESS_TEXT_SQL = "HOST({value})"
#: The microseconds of an hour - a duration of microseconds is added as whole hours and a remainder.
MICROSECONDS_PER_HOUR = 3_600 * MICROSECONDS_PER_SECOND
