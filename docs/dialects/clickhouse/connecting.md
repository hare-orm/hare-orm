# Connecting to ClickHouse

hare runs on ClickHouse, the columnar database for analytics, over either of the server's two
interfaces — each through a library of its own:

| `engine` / DB_URL scheme | Driver | Install |
|---|---|---|
| `clickhouse+clickhouse-connect` | [clickhouse-connect](https://clickhouse.com/docs/integrations/python)'s asyncio client, over the HTTP interface (port 8123) | `pip install "hare-orm[clickhouse]"` |
| `clickhouse+clickhouse-driver` | [clickhouse-driver](https://clickhouse-driver.readthedocs.io/), over the native TCP protocol (port 9000) | `pip install "hare-orm[clickhouse-driver]"` |

```python
await Hare.init(
    config={
        "connections": {"analytics": "clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics"},
        "apps": {"events": {"models": ["myapp.events"], "default_connection": "analytics"}},
    }
)
```

The native protocol takes the other scheme and port, and nothing else changes — the models, the
queries and the SQL are the same:

```python
"connections": {"analytics": "clickhouse+clickhouse-driver://default:secret@clickhouse.local:9000/analytics"}
```

The dialect is one of hare's built-in dialects, built only from hare's dialect hooks and `Features`,
the way a dialect of another package would be; like the other built-in ones, it is imported only when a
connection or a lookup names it. It runs on ClickHouse 24.3 and later.

The URL's path names the database; the port is the one of the driver's interface — 8123 by default
for `clickhouse+clickhouse-connect`, 9000 for `clickhouse+clickhouse-driver`.

| Setting | Meaning |
|---|---|
| `secure` | TLS: HTTPS instead of HTTP, the native protocol's secure port (9440 on a default server — name it in the URL). Default `false`. |
| `compress` | Compressed data: the HTTP responses, the blocks of the native protocol (LZ4). Default `true`. |
| `connect_timeout` | Seconds to wait for a connection, above 0 and up to 3600. Default 10. |
| `send_receive_timeout` | Seconds a statement may take, above 0 and up to 86400. Default 300. |
| `native_port` | The port of the native protocol `hare dbshell` runs `clickhouse-client` on, 1 to 65535. Default: the client's own (9000, 9440 with `secure`) on `clickhouse+clickhouse-connect`, the connection's own port on `clickhouse+clickhouse-driver`. |
| `max_size` | `clickhouse+clickhouse-driver` only: the most connections the client keeps — the statements it runs at once — 1 to 1024. Default 16. |
| `cluster` | The name of the cluster the server belongs to: every statement of the schema runs `ON CLUSTER` it, and the journal of migrations is replicated — see [Cluster](schema-objects.md#cluster). Default none. |
| `async_insert` | The server collects the inserts of many statements and writes them together (`async_insert=1`) — for many small inserts. Default `false`. |
| `wait_for_async_insert` | With `async_insert`: an insert returns once its rows are written, its error raised (`wait_for_async_insert=1`); `false` returns at once, before a row is written and with no error. Default `true`. |
| `transactions` | `atomic()` runs a ClickHouse transaction — on a server with a ClickHouse Keeper and `allow_experimental_transactions`, checked when the connection opens. See [Transactions and locks](transactions-and-locks.md). Default `false`. |
| `keeper_hosts` | With `transactions=true`: the ClickHouse Keeper servers `select_for_update()` locks rows in, `host[:port]` separated by commas (port 9181 by default). Default none. |

Every statement runs with `join_use_nulls=1` (a LEFT JOIN without a matching row reads NULLs, as in
SQL), `mutations_sync=2` and `lightweight_deletes_sync=2` (an UPDATE or DELETE has finished when it
returns) and `session_timezone='UTC'`. A server without `lightweight_deletes_sync` — ClickHouse 24.3
has none — runs without it: its lightweight DELETE waits as `mutations_sync` says. Every value of a
statement is written into its text, so the server's limits on the size of a statement are raised
for it: `max_query_size` to 1 GiB, `max_ast_elements` and `max_expanded_ast_elements` to
100,000,000 — the defaults (256 KiB of text) refuse a long `__in` list or an UPDATE writing a
large JSON document.

A date or a moment is stored from 1900-01-01 to 2299-12-31 (`Date32`, `DateTime64`); the server
turns any other one into the nearest of them without an error. hare refuses such a value instead —
in a write and in a filter alike — with `ValidationError`.

`bulk_create()` loads a batch in one binary insert (`Features.copies_bulk_inserts`) — see
[Bulk loads](differences.md#bulk-loads) — and model rows and `values()` rows are read off the
tuples the library gives, without a mapping per row (`Features.supports_positional_rows`).

## clickhouse-connect: the HTTP interface

Each statement is an HTTP request; clickhouse-connect keeps the HTTP connections and runs the
statements of concurrent tasks side by side.

## clickhouse-driver: the native protocol

clickhouse-driver is a synchronous library, and a connection of the native protocol runs one
statement at a time. hare runs each statement on a worker thread and the event loop waits for it
without blocking; every worker thread keeps a connection of its own, opened by its first statement.
`max_size` is the number of threads: that many statements run at once, the others wait their turn.

- **Pool status.** `get_pool_status()` reports the connections open, taken and waited for, and
  the pool metrics count them (`Features.supports_pool_status`).
- **A cancelled statement.** Cancelling a task drops its statement when it has not started yet. A
  statement already sent runs to its end on the server — its result is thrown away.
- **Closing.** `close()` waits up to 10 seconds for the statements still running. After that the
  ones not started fail with `DBConnectionError`, and a statement still running ends on its own —
  its thread closes its connection then.
- **A failed statement.** The library closes the connection of a statement that failed; the next
  statement of that thread opens another one.
- **The ping.** The library asks the server whether the connection is alive before every
  statement — one more round trip each time. hare sends that ping only on a connection idle for
  more than 10 seconds: a busy connection starts its statement at once, and one the server closed
  while it sat idle is opened again unnoticed.
- **Moments.** A binary insert writes each moment as the whole ticks of the server's column
  (`DateTime64` of any precision, `DateTime`), and a `DateTime64` is read from its ticks the same
  way — exactly, before 1970 and up to 2299 alike. The library itself goes through a
  floating-point number of seconds, a microsecond off past 2242, and Windows refuses its
  conversion of any moment before 1970.
- **`compress`** needs the `lz4` and `clickhouse-cityhash` packages, which the
  `hare-orm[clickhouse-driver]` extra installs. For a server on the same machine
  `compress=false` saves the work of compressing.

Which one to take: a statement takes about the same time on both — the server's work and the
library's own reading of the answer decide it, not the transport (see the
[benchmark](../../benchmarks/clickhouse.md)). The native protocol opens a connection per worker
thread and reports its pool; the HTTP interface passes through proxies and load balancers that
speak only HTTP.
