from __future__ import annotations

import datetime
import decimal
import inspect

import asyncpg

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType

#: Start of asyncpg's InterfaceError message for a statement binding more parameters than the
#: Postgres protocol allows - raised before anything is sent, the connection stays usable.
ASYNCPG_TOO_MANY_ARGUMENTS_MESSAGE = "the number of query arguments cannot exceed"

#: How long to wait for the connection pool to close gracefully before terminating it.
POOL_CLOSE_TIMEOUT_SECONDS = 10

#: Types AsyncpgClient._asyncpg_bind_value() can identify as never duck-type-matching
#: hare.dialects.postgresql.fields.ranges.Range (none of them can carry a lower_inc attribute) via a single
#: type() membership check, skipping that check's 1-4 hasattr() calls for the common case.
FAST_SCALAR_BIND_TYPES = frozenset(
    {bytes, str, int, float, bool, type(None), decimal.Decimal, datetime.datetime, datetime.date, datetime.time}
)
#: The FAST_SCALAR_BIND_TYPES asyncpg takes exactly as they are - not a time (a naive one gets an
#: offset) nor a datetime (a naive pre-1970 one may get the system zone). A row of only these is
#: bound without adapting its values one by one.
PLAIN_BIND_TYPES = FAST_SCALAR_BIND_TYPES - {datetime.datetime, datetime.time}
#: PLAIN_BIND_TYPES as the tuple ``rust.native.rows.are_all_of_types()`` takes.
PLAIN_BIND_TYPE_TUPLE = tuple(PLAIN_BIND_TYPES)

#: Origin and infinities of Postgres's binary ``timestamp`` - microseconds since 2000-01-01.
POSTGRES_EPOCH = datetime.datetime(2000, 1, 1)
POSTGRES_TIMESTAMP_INFINITY = 2**63 - 1
POSTGRES_TIMESTAMP_NEGATIVE_INFINITY = -(2**63)

#: A naive datetime before this year may be one ``datetime.astimezone()`` can't convert (Windows).
EPOCH_YEAR = 1970

#: AsyncpgTransactionClient.stream()'s default `Connection.cursor(..., prefetch=...)`
#: batch size when QuerySet.stream()'s own chunk_size argument is left unset (0) - matches
#: iterator()'s own default chunk_size, so the two methods behave similarly out of the box.
DEFAULT_STREAM_PREFETCH = 1000

#: Connection parameters only asyncpg.create_pool() accepts - left out of the dedicated
#: asyncpg.connect() connection listen() opens, which rejects them with a TypeError.
ASYNCPG_POOL_ONLY_PARAMETERS = frozenset(
    {"min_size", "max_size", "max_queries", "max_inactive_connection_lifetime", "setup", "init", "reset", "connect"}
)

#: Every keyword asyncpg.create_pool() accepts, its own and those it forwards to asyncpg.connect()
#: - read from the installed asyncpg, so a credential outside it is rejected at configuration
#: time instead of by the first query.
ASYNCPG_CONNECTION_PARAMETERS = frozenset(
    name
    for function in (asyncpg.connect, asyncpg.create_pool)
    for argument_specification in (inspect.getfullargspec(function),)
    for name in (*argument_specification.args, *argument_specification.kwonlyargs)
)

#: Sanity ceilings for asyncpg's own numeric connection parameters.
ASYNCPG_MAX_QUERIES_LIMIT = 1_000_000_000
ASYNCPG_MAX_LIFETIME_SECONDS = 86400.0
ASYNCPG_MAX_CONNECT_TIMEOUT_SECONDS = 86400.0
ASYNCPG_MAX_CACHEABLE_STATEMENT_SIZE = 2**30


#: The settings asyncpg's pool takes that hare checks before passing them on.
ASYNCPG_CONNECTION_OPTIONS = ConnectionOptions(
    ConnectionOption("max_queries", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=ASYNCPG_MAX_QUERIES_LIMIT),
    ConnectionOption(
        "max_cacheable_statement_size",
        ConnectionOptionType.WHOLE_NUMBER,
        minimum=0,
        maximum=ASYNCPG_MAX_CACHEABLE_STATEMENT_SIZE,
    ),
    ConnectionOption(
        "max_inactive_connection_lifetime", ConnectionOptionType.SECONDS, maximum=ASYNCPG_MAX_LIFETIME_SECONDS
    ),
    ConnectionOption(
        "max_cached_statement_lifetime", ConnectionOptionType.SECONDS, maximum=ASYNCPG_MAX_LIFETIME_SECONDS
    ),
    ConnectionOption(
        "timeout", ConnectionOptionType.SECONDS, positive=True, maximum=ASYNCPG_MAX_CONNECT_TIMEOUT_SECONDS
    ),
)
