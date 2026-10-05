from __future__ import annotations

import re

#: Microsecond counts of the time units a timedelta is split into.
MICROSECONDS_PER_SECOND = 1_000_000
MICROSECONDS_PER_HOUR = 3_600 * MICROSECONDS_PER_SECOND
MICROSECONDS_PER_DAY = 86_400 * MICROSECONDS_PER_SECOND


#: A collation name ``Collate()`` accepts - written into the SQL text quoted, never bound.
COLLATION_NAME_PATTERN = re.compile(r"[A-Za-z0-9_@.-]+")

#: How many Query classes' shared empty builders are kept.
EMPTY_BUILDER_CACHE_SIZE = 64
#: What a log, an event or an error shows in place of a parameter holding a ``sensitive=True``
#: field's value.
HIDDEN_PARAMETER_TEXT = "<hidden>"

DEFAULT_LIKE_ESCAPE_CLAUSE = " ESCAPE '\\'"

#: Order matters - the backslash escape must be applied first, or it would double-escape
#: the backslashes just inserted for "%"/"_".
LIKE_ESCAPE_MAP = (
    ("\\", "\\\\"),
    ("%", "\\%"),
    ("_", "\\_"),
)

#: The most bytes a name hare generates - a table, column, through table, index, constraint or
#: alias name - takes: PostgreSQL's limit, the shortest among hare's own dialects. A longer name is
#: shortened with a digest (``Identifiers``). The limit is fixed rather than taken from the
#: dialects loaded, so a model gets the same names on every connection and every run; a dialect
#: keeping names shorter than this can't be registered.
IDENTIFIER_LENGTH_LIMIT = 63

#: How many hex characters of a SHA-256 digest end a generated name shortened to fit the
#: identifier length limit - 40 bits, leaving collisions between shortened names practically
#: unreachable.
IDENTIFIER_DIGEST_LENGTH = 10

#: How many shortened identifiers ``Identifiers.shorten()`` keeps.
SHORTENED_IDENTIFIER_CACHE_SIZE = 1024

#: The names a query filtered and sorted outside a derived table selects its conditions and sort keys
#: under - followed by their position.
CORRELATED_CONDITION_PREFIX = "hare_condition_"
CORRELATED_SORT_KEY_PREFIX = "hare_sort_key_"

#: What an INSERT-only builder method raises on a query that inserts nothing.
QUERY_WITHOUT_INSERT_MESSAGE = "'Query' object has no attribute 'insert'"
