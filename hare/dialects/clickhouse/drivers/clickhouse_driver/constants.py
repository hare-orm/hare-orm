from __future__ import annotations

import datetime

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_CONNECTION_OPTION_DEFAULTS
from hare.dialects.clickhouse.constants import CLICKHOUSE_CONNECTION_OPTIONS
from hare.dialects.enums import ConnectionOptionType

#: The most connections a client can be told to keep.
CLICKHOUSE_DRIVER_MAX_POOL_SIZE = 1024

#: The settings of a clickhouse-driver connection: ClickHouse's own, and the most connections the
#: client keeps - the statements it runs at once.
CLICKHOUSE_DRIVER_CONNECTION_OPTIONS = CLICKHOUSE_CONNECTION_OPTIONS + ConnectionOptions(
    ConnectionOption(
        "max_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=CLICKHOUSE_DRIVER_MAX_POOL_SIZE
    ),
)

#: The values of the connection settings left unset.
CLICKHOUSE_DRIVER_CONNECTION_OPTION_DEFAULTS = {**CLICKHOUSE_CONNECTION_OPTION_DEFAULTS, "max_size": 16}

#: How the blocks sent and received are compressed with ``compress`` on.
CLICKHOUSE_DRIVER_COMPRESSION = "lz4"

#: What the names of a client's worker threads begin with.
CLICKHOUSE_DRIVER_THREAD_NAME_PREFIX = "hare-clickhouse"

#: The seconds ``close()`` waits for the statements still running before it breaks their connections.
CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS = 10

#: The error of a statement that had not started when ``close()`` stopped waiting.
CLICKHOUSE_DRIVER_CLOSED_MESSAGE = "The connection was closed before the statement started"

#: The statement a binary insert begins with - the library sends the rows after it as blocks.
CLICKHOUSE_DRIVER_INSERT_STATEMENT_TEMPLATE = "INSERT INTO {table} ({columns}) VALUES"

#: The seconds after its last statement within which a connection starts the next one without the
#: ping the library sends first - a connection idle for longer is checked, and opened again when
#: the server closed it meanwhile.
CLICKHOUSE_DRIVER_PING_AFTER_IDLE_SECONDS = 10.0

#: The fractional digits of a second Python's ``datetime`` keeps.
CLICKHOUSE_DRIVER_MICROSECOND_DIGITS = 6
CLICKHOUSE_DRIVER_MICROSECONDS_PER_SECOND = 1_000_000
CLICKHOUSE_DRIVER_MICROSECONDS_PER_DAY = 86_400 * CLICKHOUSE_DRIVER_MICROSECONDS_PER_SECOND
#: How the server's type of a column of moments with a fraction of a second begins - the digits
#: of the fraction follow.
CLICKHOUSE_DRIVER_TICKS_MOMENT_TYPE_PREFIX = "DateTime64("
#: The server's type of a column of moments of whole seconds, and how it begins when it names a
#: time zone.
CLICKHOUSE_DRIVER_SECONDS_MOMENT_TYPE = "DateTime"
CLICKHOUSE_DRIVER_SECONDS_MOMENT_TYPE_PREFIX = "DateTime("

#: What a column type holding moments at any depth names.
CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME = "DateTime"
#: The container types a moment can be held in.
CLICKHOUSE_DRIVER_ARRAY_TYPE = "Array"
CLICKHOUSE_DRIVER_MAP_TYPE = "Map"
CLICKHOUSE_DRIVER_TUPLE_TYPE = "Tuple"
#: The moment the ticks of a column of moments count from, naive - its UTC wall clock.
CLICKHOUSE_DRIVER_NAIVE_MOMENT_EPOCH = datetime.datetime(1970, 1, 1)
#: The names of the zone whose wall clock is UTC's - a moment in it is the epoch plus its ticks.
CLICKHOUSE_DRIVER_UTC_ZONE_NAMES = frozenset({"UTC", "Etc/UTC"})

#: The geometry types the library doesn't know -> the type each is written as.
CLICKHOUSE_DRIVER_GEOMETRY_ALIASES = (
    ("LineString", "Array(Point)"),
    ("MultiLineString", "Array(LineString)"),
)
#: How a ``Variant`` column's discriminators are sent - one byte per row; the compact mode isn't read.
CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE = 0
#: The batches of a streamed query its reader may hold unread - the worker thread reading the rows waits
#: beyond them; and how often the waiting thread looks whether the reader stopped, in seconds.
CLICKHOUSE_DRIVER_STREAM_QUEUE_SIZE = 2
CLICKHOUSE_DRIVER_STREAM_WAIT_SECONDS = 0.1

#: The first column of the ProfileEvents block the server sends after each statement - read by the
#: library only to be dropped, so hare passes its values over unread.
CLICKHOUSE_DRIVER_PROFILE_EVENTS_FIRST_COLUMN = "host_name"
#: The bytes a value of a column type takes - the types of the ProfileEvents block passed over.
CLICKHOUSE_DRIVER_FIXED_VALUE_WIDTHS = {
    "UInt8": 1,
    "Int8": 1,
    "UInt16": 2,
    "Int16": 2,
    "UInt32": 4,
    "Int32": 4,
    "Float32": 4,
    "DateTime": 4,
    "UInt64": 8,
    "Int64": 8,
    "Float64": 8,
}
#: How the types whose values take a fixed width whatever their arguments begin, with that width.
CLICKHOUSE_DRIVER_FIXED_VALUE_WIDTH_PREFIXES = (("Enum8(", 1), ("Enum16(", 2))
#: The type of a column of strings, each written as its length and its bytes.
CLICKHOUSE_DRIVER_STRING_TYPE = "String"

#: The most decompressor classes kept by the compression method byte of a block - one per method.
CLICKHOUSE_DRIVER_DECOMPRESSOR_CACHE_SIZE = 8

#: How a column type with arguments begins for the types the library is taught: ``Dynamic(...)`` and
#: ``Variant(...)``.
CLICKHOUSE_DRIVER_DYNAMIC_PREFIX = "Dynamic("
CLICKHOUSE_DRIVER_VARIANT_PREFIX = "Variant("

#: The most sets of statement settings kept written - the connection's own, and those of a
#: statement streaming its rows in blocks of a size of its own.
CLICKHOUSE_DRIVER_SETTINGS_CACHE_SIZE = 64
