from __future__ import annotations

from hare.dialects.base.sql_dialect import SqlDialect

#: How many rows are read or written before yielding to the event loop.
CHUNK_SIZE = 2000


#: How many leading characters of the table name / first field name survive truncation in a
#: generated index name, before the digest suffix (INDEX_NAME_HASH_LENGTH).
INDEX_NAME_TABLE_PREFIX_LENGTH = 11
INDEX_NAME_FIELD_PREFIX_LENGTH = 7
#: 48 bits of hash keep generated index names of similarly named tables from colliding; the longest
#: name (37 chars) fits PostgreSQL's 63-byte limit.
INDEX_NAME_HASH_LENGTH = 12

#: Same idea for a generated foreign-key constraint name.
FOREIGN_KEY_NAME_TABLE_PREFIX_LENGTH = 8
FOREIGN_KEY_NAME_HASH_LENGTH = 8

#: Characters whose presence marks a "field name" as an index expression, not a plain column.
INDEX_EXPRESSION_MARKER_CHARS = ("(", ")", " ", '"', ".", ":")

#: ASCII code point -> escaped replacement, layered onto the identity table by
#: BaseSchemaEditor's comment-escaping logic.

TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


#: A query-executing call taking at least this long gets one extra DEBUG-level log line - a
#: generous default meant to flag genuinely slow queries, not every normal round-trip.
SLOW_QUERY_THRESHOLD_MS = 500.0
#: Sanity ceiling (one day, in milliseconds) for Hare.init(slow_query_threshold_ms=...).
MAX_SLOW_QUERY_THRESHOLD_MS = 86_400_000.0


#: The client methods running a statement given as ``(query, values)`` - translate_exceptions
#: attaches sql and parameters to an exception of these only. ``stream`` is observed by
#: ``TransactionClient.stream()`` itself.
QUERY_EXECUTING_METHOD_NAMES = frozenset(
    {
        "execute",
        "execute_described",
        "execute_many",
        "execute_script",
    }
)

#: Client methods ``DatabaseClient.translate_exceptions`` reports to the observers
#: (``QueryExecuted``) and runs the query wrappers around - every query-executing method and the
#: bulk COPY load.
OBSERVED_METHOD_NAMES = QUERY_EXECUTING_METHOD_NAMES | {"copy"}

#: The client methods ``command_timeout`` bounds: the query-executing ones and COPY. Not
#: begin/commit/rollback/savepoint - cancelling one leaves the transaction state unknown - nor
#: stream.
COMMAND_TIMEOUT_METHOD_NAMES = QUERY_EXECUTING_METHOD_NAMES | {"copy"}


#: The dialect of plain SQL rendered for no particular database.
SQL_DIALECT = SqlDialect()

#: Bind parameters left unused by prefetch_related()'s batched through-table ``IN`` lookups, out
#: of the backend's own per-statement ceiling - room for the through model's own tenant/soft-delete
#: filter.
PREFETCH_BIND_PARAMETERS_HEADROOM = 100

#: Most composite-key rows one prefetch_related() query filters on - each row becomes an AND-group
#: of a flat OR chain, and a chain that long can hit a backend's own expression-depth limit
#: (SQLite's is 1000) or Python's recursion limit well before the bind-parameter ceiling.
PREFETCH_MAX_COMPOSITE_ROWS = 500
