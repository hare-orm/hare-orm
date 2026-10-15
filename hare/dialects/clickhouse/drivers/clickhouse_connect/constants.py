from __future__ import annotations

import datetime
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
