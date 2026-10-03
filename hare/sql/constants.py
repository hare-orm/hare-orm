import re

#: A character no SQL statement text can carry: the Postgres protocol ends a string at it, so a
#: caller-supplied name or inline literal holding one is rejected before the statement is sent.
SQL_NULL_BYTE = "\x00"
SQL_NULL_BYTE_MESSAGE = "{text!r}: a name or literal written into the SQL text can't contain a null byte ('\\x00')"


#: Microsecond counts of the time units a timedelta is split into.
MICROSECONDS_PER_SECOND = 1_000_000
MICROSECONDS_PER_HOUR = 3_600 * MICROSECONDS_PER_SECOND
MICROSECONDS_PER_DAY = 86_400 * MICROSECONDS_PER_SECOND


#: A collation name ``Collate()`` accepts - written into the SQL text quoted, never bound.
COLLATION_NAME_PATTERN = re.compile(r"[A-Za-z0-9_@.-]+")

#: How many Query classes' shared empty builders are kept.
EMPTY_BUILDER_CACHE_SIZE = 64
