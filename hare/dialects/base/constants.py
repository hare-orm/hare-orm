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
FK_NAME_TABLE_PREFIX_LENGTH = 8
FK_NAME_HASH_LENGTH = 8

#: Characters whose presence marks a "field name" as an index expression, not a plain column.
INDEX_EXPRESSION_MARKER_CHARS = ("(", ")", " ", '"', ".", ":")

#: ASCII code point -> escaped replacement, layered onto the identity table by
#: BaseSchemaEditor's comment-escaping logic.

TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
FALSE_STRINGS = frozenset({"false", "0", "no", "off"})

#: Bind parameters one statement may carry unless the driver says otherwise - the Postgres wire
#: protocol's ceiling (the Bind message counts them in a signed 16-bit integer).
DEFAULT_MAX_BIND_PARAMETERS = 32767

#: Host (reg-name or bracketed IPv6) and optional port right after a DB_URL's userinfo "@",
#: followed by the end of the authority.
#: A bracketed IPv6 host may carry a percent-encoded zone id ("[fe80::1%25eth0]").
DB_URL_AUTHORITY_HOST_PATTERN = (
    r"(?:\[[0-9A-Fa-f:.]*(?:%25[A-Za-z0-9._~%-]+)?\]|[A-Za-z0-9._~%!$&'()*+,;=-]*)(?::[0-9]*)?(?=[/?#]|$)"
)

#: A query-executing call taking at least this long gets one extra DEBUG-level log line (see
#: each backend client's own translate_exceptions query-hook/timing wrapper) - a generous
#: default meant to flag genuinely slow queries, not every normal round-trip.
SLOW_QUERY_THRESHOLD_MS = 500.0
#: Sanity ceiling (one day, in milliseconds) for Hare.init(slow_query_threshold_ms=...).
MAX_SLOW_QUERY_THRESHOLD_MS = 86_400_000.0

#: How long a shielded COMMIT/ROLLBACK/savepoint statement is waited for after its task was
#: cancelled - a dead server must not hang the cancellation.
SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS = 30.0

#: How long an ordinary query waits for its turn while a sibling task's savepoint is open on the
#: same transaction, before raising TransactionManagementError.
AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS = 30.0

#: How long a top-level COMMIT/ROLLBACK waits for the statements and savepoints other tasks still
#: run on the same transaction (asyncio.gather()/TaskGroup siblings) before giving up - a COMMIT
#: then fails with TransactionManagementError and nothing is committed, a ROLLBACK goes ahead
#: anyway.
TRANSACTION_END_WAIT_TIMEOUT_SECONDS = 30.0

#: The bound of DatabaseClient.ping() - a server that stopped answering without closing the socket
#: never makes the driver raise.
PING_TIMEOUT_SECONDS = 5.0

#: The client methods running a statement given as ``(query, values)`` - translate_exceptions
#: attaches sql and params to an exception of these only. ``stream`` is observed by
#: ``TransactionClient.stream()`` itself.
QUERY_EXECUTING_METHOD_NAMES = frozenset(
    {
        "execute",
        "execute_described",
        "execute_many",
        "execute_script",
    }
)

#: Client methods each backend's translate_exceptions wrapper reports to the observers
#: (``QueryExecuted``) and runs the query wrappers around - every query-executing method and the
#: bulk COPY load.
OBSERVED_METHOD_NAMES = QUERY_EXECUTING_METHOD_NAMES | {"copy"}

#: The client methods ``command_timeout`` bounds: the query-executing ones and COPY. Not
#: begin/commit/rollback/savepoint - cancelling one leaves the transaction state unknown - nor
#: stream.
COMMAND_TIMEOUT_METHOD_NAMES = QUERY_EXECUTING_METHOD_NAMES | {"copy"}

#: DDL templates of the schema editor's table, key and constraint clauses. Identifier placeholders
#: have no quotes - the caller passes quoted values.
CHECK_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {name} CHECK ({check})"
UNIQUE_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {index_name} UNIQUE{nulls} ({fields}){include}"
PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {index_name} PRIMARY KEY ({fields})"
GENERATED_PK_TEMPLATE = "{field_name} {generated_sql}{comment}"
FK_TEMPLATE = " REFERENCES {table} ({field}) ON DELETE {on_delete}{comment}"
#: Table-level composite FK constraint - a composite-target FK/O2O renders N plain shadow
#: columns (no inline REFERENCES, unlike FK_TEMPLATE's single-column case) plus one of these,
#: applied after CREATE TABLE the same way Meta.constraints already are.
FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE = (
    "CONSTRAINT {name} FOREIGN KEY ({fields}) REFERENCES {table} ({to_fields}) ON DELETE {on_delete}"
)


#: The dialect of plain SQL rendered for no particular database.
SQL_DIALECT = SqlDialect()

#: Bind parameters left unused by prefetch_related()'s batched through-table ``IN`` lookups, out
#: of the backend's own per-statement ceiling - room for the through model's own tenant/soft-delete
#: filter.
PREFETCH_BIND_PARAMS_HEADROOM = 100

#: Most composite-key rows one prefetch_related() query filters on - each row becomes an AND-group
#: of a flat OR chain, and a chain that long can hit a backend's own expression-depth limit
#: (SQLite's is 1000) or Python's recursion limit well before the bind-parameter ceiling.
PREFETCH_MAX_COMPOSITE_ROWS = 500
