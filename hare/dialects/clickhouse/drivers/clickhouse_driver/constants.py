from __future__ import annotations

import datetime
from decimal import Context

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_CONNECTION_OPTION_DEFAULTS
from hare.dialects.clickhouse.constants import CLICKHOUSE_CONNECTION_OPTIONS
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.enums import ConnectionOptionType

CLICKHOUSE_DRIVER_COLUMN_WRITE_ERROR = 'Cannot encode column "{name}" as {type}: {error}'

#: The loggers of the clickhouse-driver library.
CLICKHOUSE_DRIVER_LOGGER_NAMES = ("clickhouse_driver",)

#: The settings of a clickhouse-driver connection: ClickHouse's own, and the most connections the
#: client keeps - the statements it runs at once.
CLICKHOUSE_DRIVER_CONNECTION_OPTIONS = CLICKHOUSE_CONNECTION_OPTIONS + ConnectionOptions(
    ConnectionOption("max_size", ConnectionOptionType.WHOLE_NUMBER),
)

#: The values of the connection settings left unset.
CLICKHOUSE_DRIVER_CONNECTION_OPTION_DEFAULTS = {**CLICKHOUSE_CONNECTION_OPTION_DEFAULTS, "max_size": 16}

#: The logger clickhouse-driver's connection writes to.
CLICKHOUSE_DRIVER_LIBRARY_LOGGER_NAME = "clickhouse_driver.connection"

#: The formats of the records the library writes of a statement it sends - the statement's whole text,
#: the values written into it.
CLICKHOUSE_DRIVER_STATEMENT_LOG_FORMATS = frozenset({"Query: %s"})

#: How the blocks sent and received are compressed with ``compress`` on.
CLICKHOUSE_DRIVER_COMPRESSION = "lz4"
#: The setting naming how the server compresses the blocks it sends, and its value matching
#: ``CLICKHOUSE_DRIVER_COMPRESSION`` - the server's default is ZSTD from ClickHouse 26.9 on.
CLICKHOUSE_DRIVER_NETWORK_COMPRESSION_SETTING = "network_compression_method"
CLICKHOUSE_DRIVER_NETWORK_COMPRESSION_METHOD = "LZ4"

#: What the names of a client's worker threads begin with.
CLICKHOUSE_DRIVER_THREAD_NAME_PREFIX = "hare-clickhouse"

#: The grace period before queued statements are cancelled; running IO retains its connection.
CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS = 10

#: The error of a statement submitted after shutdown started or started after its grace period.
CLICKHOUSE_DRIVER_CLOSED_MESSAGE = "The connection was closed before the statement started"

#: The statement a binary insert begins with - the library sends the rows after it as blocks.
CLICKHOUSE_DRIVER_INSERT_STATEMENT_TEMPLATE = "INSERT INTO {table} ({columns}) VALUES"


#: The fractional digits of a second Python's ``datetime`` keeps.
CLICKHOUSE_DRIVER_MICROSECOND_DIGITS = 6
#: What a column type holding moments at any depth names.
CLICKHOUSE_DRIVER_MOMENT_TYPE_NAME = ClickhouseTypeName.DATETIME
#: The moment the ticks of a column of moments count from, naive - its UTC wall clock.
CLICKHOUSE_DRIVER_NAIVE_MOMENT_EPOCH = datetime.datetime(1970, 1, 1)
#: The names of the zone whose wall clock is UTC's - a moment in it is the epoch plus its ticks.
CLICKHOUSE_DRIVER_UTC_ZONE_NAMES = frozenset({"UTC", "Etc/UTC"})

#: The geometry types the library doesn't know -> the type each is written as.
CLICKHOUSE_DRIVER_GEOMETRY_ALIASES = (
    (ClickhouseTypeName.LINE_STRING, "Array(Point)"),
    (ClickhouseTypeName.MULTI_LINE_STRING, "Array(LineString)"),
)
#: How a ``Variant`` column's discriminators are sent - one byte per row; the compact mode isn't read.
CLICKHOUSE_DRIVER_VARIANT_BASIC_MODE = 0
#: The batches of a streamed query its reader may hold unread - the worker thread reading the rows waits
#: beyond them; and how often the waiting thread looks whether the reader stopped, in seconds.
CLICKHOUSE_DRIVER_STREAM_QUEUE_SIZE = 2
CLICKHOUSE_DRIVER_STREAM_WAIT_SECONDS = 0.1


#: How a column type with arguments begins for the types the library is taught: ``Dynamic(...)`` and
#: ``Variant(...)``.
CLICKHOUSE_DRIVER_DYNAMIC_PREFIX = "Dynamic("
CLICKHOUSE_DRIVER_VARIANT_PREFIX = "Variant("


#: The context a decimal column's values are built in - the precision of ClickHouse's widest decimal.
CLICKHOUSE_DRIVER_DECIMAL_CONTEXT = Context(prec=76)

#: What the library puts between the server's message of an error and the server's stack trace.
CLICKHOUSE_DRIVER_STACK_TRACE_SEPARATOR = ". Stack trace:"
