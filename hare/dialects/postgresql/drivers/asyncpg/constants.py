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
#: The same origin of ``timestamptz``, a UTC moment.
POSTGRES_EPOCH_UTC = POSTGRES_EPOCH.replace(tzinfo=datetime.UTC)
#: Origin and infinities of Postgres's binary ``date`` - days since 2000-01-01.
POSTGRES_EPOCH_DATE_ORDINAL = POSTGRES_EPOCH.toordinal()
POSTGRES_DATE_INFINITY = 2**31 - 1
POSTGRES_DATE_NEGATIVE_INFINITY = -(2**31)
#: The sizes of a binary ``date`` and ``timestamptz``.
POSTGRES_DATE_SIZE = 4
POSTGRES_TIMESTAMP_SIZE = 8

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


#: The root certificate setting of a DB_URL - asyncpg reads it only from a DSN, as ``sslrootcert``.
ASYNCPG_ROOT_CERTIFICATE_SETTING = "ssl_root_cert"
#: The DSN carrying the root certificate - the host, port and the rest are passed apart, over it.
ASYNCPG_ROOT_CERTIFICATE_DSN_PREFIX = "postgresql://?"
ASYNCPG_ROOT_CERTIFICATE_DSN_PARAMETER = "sslrootcert"

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
    ConnectionOption(ASYNCPG_ROOT_CERTIFICATE_SETTING, ConnectionOptionType.TEXT),
)

#: How asyncpg's client-side refusal of a statement given another number of arguments than it takes
#: begins - an error of the statement, not of the connection.
ASYNCPG_ARGUMENT_COUNT_MESSAGE_PREFIX = "the server expects "

#: The frame of asyncpg's reading of a simple query's result - it fails on the empty query response
#: of a script of comments only, which has no status.
ASYNCPG_SIMPLE_QUERY_RESULT_FRAME_NAME = "_on_result__simple_query"
#: The frame of asyncpg's decoding of a result value - a ValueError or an OverflowError raised under
#: it is a value Python's type holds none of ('infinity'::interval, 24:00, a year past 9999).
ASYNCPG_DECODE_FRAME_NAME_PREFIX = "asyncpg.protocol.protocol.Codec.decode"

#: How asyncpg turns an interval's months into days - a year of 365 days, a month of 30.
POSTGRES_INTERVAL_MONTHS_PER_YEAR = 12
POSTGRES_INTERVAL_DAYS_PER_YEAR = 365
POSTGRES_INTERVAL_DAYS_PER_MONTH = 30

#: Types of PostgreSQL extensions asyncpg reads as text - an array of them holding an empty value
#: crashes asyncpg's text array parser, so every connection reads them through hare's binary codecs.
ASYNCPG_EXTENSION_TYPE_NAMES = ("citext", "ltree", "hstore")
#: The extension types of ASYNCPG_EXTENSION_TYPE_NAMES the database has, with their schemas.
ASYNCPG_EXTENSION_TYPES_SQL = (
    "SELECT pg_type.typname, pg_namespace.nspname FROM pg_type "
    "JOIN pg_namespace ON pg_namespace.oid = pg_type.typnamespace "
    "WHERE pg_type.typname = ANY($1::text[]) AND pg_type.typtype = 'b'"
)
#: The word of a statement creating or dropping an extension - its types are looked up again after it.
EXTENSION_STATEMENT_WORD = "EXTENSION"
#: The version byte leading PostgreSQL's binary ``ltree``.
LTREE_BINARY_VERSION = 1
#: The length of a NULL value in PostgreSQL's binary ``hstore``.
HSTORE_NULL_LENGTH = -1

#: PostgreSQL's limits of a ``tsvector``: a lexeme's bytes (exclusive), the whole value's bytes, a
#: lexeme's positions, and the highest position - a higher one is lowered to it.
TSVECTOR_LEXEME_BYTES_LIMIT = 2047
TSVECTOR_MAX_BYTES = (1 << 20) - 1
TSVECTOR_MAX_POSITIONS = 256
TSVECTOR_MAX_POSITION = 16383
#: The characters ``tsvector`` input separates lexemes by.
TSVECTOR_WHITESPACE = frozenset(" \t\n\r\v\f")
#: A position's weight letter in ``tsvector`` input -> the weight's bits; ``*`` is ``A``.
TSVECTOR_WEIGHTS_BY_LETTER = {"a": 3, "A": 3, "*": 3, "b": 2, "B": 2, "c": 1, "C": 1, "d": 0, "D": 0}
#: A weight's bits -> its letter in ``tsvector`` output; ``D`` is written as nothing.
TSVECTOR_LETTERS_BY_WEIGHT = ("", "C", "B", "A")
#: Where a ``tsvector`` position's weight starts in its 16 bits.
TSVECTOR_WEIGHT_SHIFT = 14
