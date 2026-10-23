from __future__ import annotations

import datetime
import decimal
import re
from types import NoneType

#: The largest response body a read through clickhouse-connect parses on the event loop - a larger one
#: is parsed on a worker thread, as the library parses every response.
CLICKHOUSE_CONNECT_INLINE_PARSE_MAX_BYTES = 64 * 1024

#: The library's functions a read taking its whole response is made of, by module - the library's
#: own query() runs where the installed version lacks one.
CLICKHOUSE_CONNECT_WHOLE_RESPONSE_FUNCTIONS = (
    ("clickhouse_connect.driver._backend.http_async", ("_plan_files", "_read_request_retryable", "release_lease")),
    (
        "clickhouse_connect.driver._backend.httpcommon",
        ("decompress_response", "plan_query_request", "summary_from_headers"),
    ),
    ("clickhouse_connect.driver._backend.models", ("QueryRuntime",)),
    ("clickhouse_connect.driver.asyncclient", ("BytesSource", "_query_is_read_only")),
    ("clickhouse_connect.driver.ctypes", ("RespBuffCls",)),
)
#: The library client's own parts such a read uses.
CLICKHOUSE_CONNECT_WHOLE_RESPONSE_CLIENT_METHODS = ("_prep_query", "_validate_settings", "_check_tz_change")

#: What the library's error says when the server failed a statement midway through its response and
#: the error's text, split between blocks of the stream, couldn't be read back.
CLICKHOUSE_CONNECT_UNREADABLE_SERVER_ERROR_MARK = "unrecognized data found in stream"

#: The settings of a statement sent through clickhouse-connect's command() - the server answers once the
#: statement is done, so a statement ON CLUSTER failing on one of the servers answers with its error,
#: not with a response broken off after the status of the servers that succeeded.
CLICKHOUSE_CONNECT_COMMAND_SETTINGS = {"wait_end_of_query": 1}

#: The response header naming the time zone the server read the moments of the response in.
CLICKHOUSE_CONNECT_TIME_ZONE_HEADER = "X-ClickHouse-Timezone"
#: The response header naming the compression of the body.
CLICKHOUSE_CONNECT_ENCODING_HEADER = "Content-Encoding"

#: The types of the values of a column of moments whose ticks are worked out before the library writes it -
#: a column holding anything else is the library's to write.
CLICKHOUSE_CONNECT_MOMENT_COLUMN_TYPES = frozenset({datetime.datetime, NoneType})

#: The most tables whose column types an insert keeps - one entry per table and list of columns.
CLICKHOUSE_CONNECT_INSERTED_COLUMN_TYPES_CACHE_SIZE = 512

#: A statement that may change a schema - the column types inserts keep are read again after it.
CLICKHOUSE_CONNECT_SCHEMA_CHANGE_PATTERN = re.compile(
    r"\s*(?:CREATE|DROP|ALTER|RENAME|EXCHANGE|ATTACH|DETACH|REPLACE|UNDROP)\b", re.IGNORECASE
)

#: The format a read with ``WITH TOTALS`` is taken in - the server sends the totals row of a ``Native``
#: response over HTTP nowhere, of a JSON one in its ``totals``.
CLICKHOUSE_CONNECT_TOTALS_FORMAT = "JSONCompact"
#: The settings of that read - each value written as the ``Native`` reading reads it back: no number
#: quoted but a 64-bit integer, a NaN or an infinity as its text.
CLICKHOUSE_CONNECT_TOTALS_SETTINGS = {"output_format_json_quote_denormals": 1, "output_format_json_quote_decimals": 0}
#: The context a decimal of the widest ClickHouse type is rounded to its scale in.
CLICKHOUSE_DECIMAL_CONTEXT = decimal.Context(prec=100)
