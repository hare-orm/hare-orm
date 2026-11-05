from __future__ import annotations

import datetime
import decimal
from types import NoneType

#: What the library's error says when the server failed a statement midway through its response and
#: the error's text, split between blocks of the stream, couldn't be read back.
CLICKHOUSE_CONNECT_UNREADABLE_SERVER_ERROR_MARK = "unrecognized data found in stream"

#: The settings of a statement sent through clickhouse-connect's command() - the server answers once the
#: statement is done, so a statement ON CLUSTER failing on one of the servers answers with its error,
#: not with a response broken off after the status of the servers that succeeded.
CLICKHOUSE_CONNECT_COMMAND_SETTINGS = {"wait_end_of_query": 1}

#: The server's error of a statement sent under the id of one still running - hare gives no statement an
#: id, so it is clickhouse-connect sending a read again after its connection broke off (it does so once
#: whatever its retries): the broken connection is the error.
CLICKHOUSE_CONNECT_REPEATED_QUERY_ERROR_CODE = 216


#: The types of the values of a column of moments whose ticks are worked out before the library writes it -
#: a column holding anything else is the library's to write.
CLICKHOUSE_CONNECT_MOMENT_COLUMN_TYPES = frozenset({datetime.datetime, NoneType})

#: The most tables whose column types an insert keeps - one entry per table and list of columns.
CLICKHOUSE_CONNECT_INSERTED_COLUMN_TYPES_CACHE_SIZE = 512

#: The format a read with ``WITH TOTALS`` is taken in - the server sends the totals row of a ``Native``
#: response over HTTP nowhere, of a JSON one in its ``totals``.
CLICKHOUSE_CONNECT_TOTALS_FORMAT = "JSONCompact"
#: The settings of that read - each value written as the ``Native`` reading reads it back: no number
#: quoted but a 64-bit integer, a NaN or an infinity as its text.
CLICKHOUSE_CONNECT_TOTALS_SETTINGS = {"output_format_json_quote_denormals": 1, "output_format_json_quote_decimals": 0}
#: The context a decimal of the widest ClickHouse type is rounded to its scale in.
CLICKHOUSE_DECIMAL_CONTEXT = decimal.Context(prec=100)
