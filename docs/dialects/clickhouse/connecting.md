# ClickHouse: connecting

hare runs on ClickHouse, the columnar database for analytics, over either of the server's two interfaces
— each through a library of its own:

| `engine` / database URL scheme | Driver | Install |
|---|---|---|
| `clickhouse+clickhouse-connect` | [clickhouse-connect](https://clickhouse.com/docs/integrations/python)'s asyncio client, over the HTTP interface (port 8123) | `pip install "hare-orm[clickhouse-connect]"` |
| `clickhouse+clickhouse-driver` | [clickhouse-driver](https://clickhouse-driver.readthedocs.io/), over the native TCP protocol (port 9000) | `pip install "hare-orm[clickhouse-driver]"` |

```python
await Hare.init(
    config={
        "connections": {"analytics": "clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics"},
        "apps": {"analytics": {"models": ["library.analytics"], "default_connection": "analytics"}},
    }
)
```

The native protocol takes the other scheme and port, and nothing else changes — the models, the queries
and the SQL are the same:

```python
config = {"connections": {"analytics": "clickhouse+clickhouse-driver://default:secret@clickhouse.local:9000/analytics"}}
```

The dialect is one of hare's built-in dialects, built only from hare's dialect hooks and `Features`, the
way a dialect of another package would be; like the other built-in ones, it is imported only when a
connection or a lookup names it. It runs on ClickHouse 25.8 and later.

## <a id="settings"></a>Settings

The URL's path names the database; the port is the one of the driver's interface — 8123 by default for
`clickhouse+clickhouse-connect`, 9000 for `clickhouse+clickhouse-driver`.

| Setting | Meaning |
|---|---|
| `secure` | TLS: HTTPS instead of HTTP, the native protocol's secure port (9440 on a default server — name it in the URL). Default `false`. |
| `compress` | Compressed data: the HTTP responses, the blocks of the native protocol (LZ4). Default `true`. |
| `connect_timeout` | Seconds to wait for a connection, above 0 and up to 3600. Default 10. |
| `send_receive_timeout` | Seconds a statement may take, above 0 and up to 86400. Default 300. The server stops a statement running longer (`max_execution_time`, `OperationalError` of code 159) over either driver — the HTTP client waits a second more for its answer; a statement's own `settings(max_execution_time=...)` takes precedence. |
| `native_port` | The port of the native protocol `hare dbshell` runs `clickhouse-client` on, 1 to 65535. Default: the client's own (9000, 9440 with `secure`) on `clickhouse+clickhouse-connect`, the connection's own port on `clickhouse+clickhouse-driver`. |
| `max_size` | `clickhouse+clickhouse-driver` only: the most connections the client keeps — the statements it runs at once — 1 to 1024. Default 16. |
| `cluster` | The name of the cluster the server belongs to: every statement of the schema runs `ON CLUSTER` it, and the journal of migrations is replicated — see [Cluster](schema-objects.md#cluster). Default none. |
| `async_insert` | The server collects the inserts of many statements and writes them together (`async_insert=1`) — for many small inserts. Default `false`. |
| `wait_for_async_insert` | With `async_insert`: an insert returns once its rows are written, its error raised (`wait_for_async_insert=1`); `false` returns at once, before a row is written and with no error. Default `true`. |
| `transactions` | `atomic()` runs a ClickHouse transaction — on a server with a ClickHouse Keeper and `allow_experimental_transactions`, checked when the connection opens. See [Transactions and locks](transactions-and-locks.md). Default `false`. |
| `keeper_hosts` | The ClickHouse Keeper servers hare takes `migrate`'s [migration lock](../../migrations/migrations.md#migration-lock) in, `host[:port]` separated by commas (port 9181 by default). A session is opened when the connection opens — `ConfigurationError` when none does. Default none. |

## <a id="statement-settings"></a>What every statement runs with

Every statement runs with these settings:

| Setting | Why |
|---|---|
| `join_use_nulls=1` | A LEFT JOIN without a matching row reads NULLs, as in SQL, not its columns' defaults. |
| `mutations_sync=2`, `lightweight_deletes_sync=2` | An UPDATE or DELETE has finished when it returns. |
| `session_timezone='UTC'` | Moments are read and written in UTC. |
| `max_query_size=1 GiB`, `max_ast_elements` and `max_expanded_ast_elements=100,000,000` | Almost every value of a statement is written into its text (a long `__in` list of a read goes as an external table — see [Long `__in` lists](differences.md#long-in-lists)); the defaults (256 KiB of text) refuse a long `__in` list of a write or an UPDATE writing a large JSON document. |
| `allow_simdjson=0` | A JSON text is read by the parser taking an integer beyond 64 bits as a number — the default one reads no path of a document holding one. |
| `input_format_json_read_bools_as_numbers=0` | A JSON boolean stays a boolean — an array of numbers and booleans would hold `true` as `1`. |
| `input_format_try_infer_dates=0`, `input_format_try_infer_datetimes=0` | A JSON string looking like a date or a moment stays a string — a JSON column would hold it as a date, its offset lost. |
| `cast_string_to_dynamic_use_inference=0`, `cast_string_to_variant_use_inference=0` | A text cast to a `Dynamic` or `Variant` value stays a text — the server would read `'5'` as a number. |
| `json_type_escape_dots_in_keys=1` | A dot in a key of a JSON value is part of the key, not a nested path. |
| `allow_suspicious_types_in_order_by=1` | A `Dynamic` value (a JSON path) can be ordered by. |
| `calculate_text_stack_trace=0` | An error's text is the server's message, without the server's own stack trace the native protocol sends. |

A wrong password or user and a missing database of the connection raise `ConfigurationError` on both
drivers, as on PostgreSQL — no retry fixes them; a server that refuses or drops the connection raises
`DBConnectionError`.

## <a id="values-and-rows"></a>Values and rows

A date or a moment is stored from 1900-01-01 to 2299-12-31 (`Date32`, `DateTime64`); the server turns any
other one into the nearest of them without an error. hare refuses such a value instead — in a write and
in a filter alike — with `ValidationError`. The range is the UTC one: a moment within a day of its ends,
read in a zone that puts its local time outside it (`1900-01-01 00:00` UTC in `America/New_York`), is
turned around by the server in `Extract()`, `Trunc()` and a date-part filter in that zone — keep such
moments a day inside the range.

`bulk_create()` loads a batch in one binary insert (`Features.copies_bulk_inserts`) — see
[Bulk loads](differences.md#bulk-loads) — and model rows and `values()` rows are read off the tuples the
library gives, without a mapping per row (`Features.supports_positional_rows`).

## <a id="http-interface"></a>The HTTP interface: `clickhouse-connect`

Each statement is an HTTP request; clickhouse-connect keeps the HTTP connections and runs the statements
of concurrent tasks side by side. It shows no one its connections: `get_pool_status()` raises
`UnSupportedError` (`Features.supports_pool_status` false), and the pool metrics leave the connection
out.

## <a id="native-protocol"></a>The native protocol: `clickhouse-driver`

clickhouse-driver is a synchronous library, and a connection of the native protocol runs one statement at
a time. hare runs each statement on a worker thread and the event loop waits for it without blocking;
every worker thread keeps a connection of its own, opened by its first statement. `max_size` is the
number of threads: that many statements run at once, the others wait their turn.

- **Pool status.** `get_pool_status()` reports the connections open, taken and waited for, and the pool
  metrics count them (`Features.supports_pool_status`).
- **A cancelled statement.** Cancelling a task drops its statement when it has not started yet. A
  statement already sent runs to its end on the server — its result is thrown away.
- **Closing.** `close()` waits up to 10 seconds for the statements still running. After that the ones not
  started fail with `DBConnectionError`, and a statement still running ends on its own — its thread
  closes its connection then.
- **A failed statement.** The library closes the connection of a statement that failed; the next
  statement of that thread opens another one.
- **The ping.** The library asks the server whether the connection is alive before every statement — one
  more round trip each time. hare sends that ping only on a connection idle for more than 10 seconds: a
  busy connection starts its statement at once, and one the server closed while it sat idle is opened
  again unnoticed.
- **Moments.** A binary insert writes each moment as the whole ticks of the server's column (`DateTime64`
  of any precision, `DateTime`), and a `DateTime64` is read from its ticks the same way — exactly, before
  1970 and up to 2299 alike. The library itself goes through a floating-point number of seconds, a
  microsecond off past 2242, and Windows refuses its conversion of any moment before 1970.
- **`compress`** needs the `lz4` and `clickhouse-cityhash` packages, which the
  `hare-orm[clickhouse-driver]` extra installs. For a server on the same machine `compress=false` saves
  the work of compressing.

## <a id="choosing-a-driver"></a>Choosing a driver

A statement takes about the same time on both — the server's work and the library's own reading of the
answer decide it, not the transport (see the [benchmark](../../benchmarks/clickhouse.md)). The native
protocol opens a connection per worker thread and reports its pool; the HTTP interface passes through
proxies and load balancers that speak only HTTP.
