# Connections

What a connection's URL and settings choose: the PostgreSQL driver, how a connection survives a
restart or a failover, credentials that rotate, TLS and the other URL parameters, the SQLite
parameters, and `Connections`, which holds the open connections of a context.

## <a id="choosing-a-postgres-driver"></a>Choosing a PostgreSQL driver

A connection picks its driver with `engine` — the same word as the scheme of its DB URL:

| `engine` / DB URL scheme | Driver |
|---|---|
| `sqlite+aiosqlite` | SQLite through `aiosqlite` over the standard `sqlite3` module |
| `postgresql` | PostgreSQL through hare's Rust driver |
| `postgresql+asyncpg` | PostgreSQL through `asyncpg` (install `hare-orm[asyncpg]`) |
| `clickhouse+clickhouse-connect` | ClickHouse through clickhouse-connect, over HTTP (install `hare-orm[clickhouse]`) |
| `clickhouse+clickhouse-driver` | ClickHouse through clickhouse-driver, over the native TCP protocol (install `hare-orm[clickhouse-driver]`) |

A driver's scheme is `<dialect>+<driver>`. The plain dialect scheme belongs to hare's own engine
of the dialect — `postgresql://` is hare's Rust driver; `sqlite://` and `clickhouse://` pick no
driver.

```python
"reporting": {"engine": "postgresql+asyncpg", "credentials": {"host": "...", ...}},
"analytics": "postgresql://user:pass@host:5432/db",
```

If the Rust extension is missing — the pure wheel on a platform without a prebuilt wheel, or a
source checkout that hasn't been built — a `postgresql` connection raises `ConfigurationError`
naming the `maturin develop` command that builds it; `postgresql+asyncpg` needs no extension.

The Rust driver runs its connections on up to four worker threads of its own, no more than the
machine's cores: the queries are started by the one thread running the event loop, and a few
workers keep up with it — while one decodes a large result, the others still serve the other
connections; many more would wait for work on cores the event loop and the database need. Set
the number with tokio's own environment variable before hare is imported:

```bash
TOKIO_WORKER_THREADS=4 python app.py
```

## <a id="connection-resilience"></a>Connection resilience (PostgreSQL only)

Both PostgreSQL drivers accept these as extra DSN query params (`?connect_max_retries=3&...`) or
`credentials` dict keys, the same way `max_size` is passed. Retry counts are whole numbers from `0` to
`100`, backoff bases finite numbers of seconds from `0` to `3600`; anything else (`"inf"`, `"nan"`, a
negative value) raises `ConfigurationError` at configuration time:

| Param | Default | Meaning |
|---|---|---|
| `connect_max_retries` | `0` | Retries for the *initial* pool connection, exponential backoff (`connect_retry_backoff_base_seconds * 2**attempt`). `0` (the default) means the first attempt's exception propagates unchanged. |
| `connect_retry_backoff_base_seconds` | `0.1` | Backoff base for the above. |
| `port` | `5432` | A whole number from `1` to `65535`, otherwise `ConfigurationError`. |
| `statement_cache_size` | driver default | A whole number from `0` to `1000000`. `0` turns the cache off: each statement is prepared for its own run — on `rust_pg` a statement whose values all carry a type of their own (a UUID, a date, a time, an aware `datetime`, a `timedelta`, a `Decimal`, a `float`, `bytes`, a `bool`, `None`) goes out with them in one round trip instead. |
| `transaction_pooling` | `false` | The connection goes through a pooler in transaction pooling (PgBouncer) — see [Connection poolers](connection-poolers.md). |
| `direct_host` / `direct_port` | none / `port` | The server's own address past the pooler, for LISTEN and session settings. Only with `transaction_pooling`. |
| `read_retry_max_retries` | `0` | Retries a query lost to a connection dropping **mid-query** (RDS failover, a network blip), not just at connect time — same exponential backoff shape, its own independent retry budget. Only ever applies to a statically read-only query (`AwaitableQuery.is_read_only`) run **outside** an explicit transaction — a write is never retried blind (it might have already landed), and a query inside `Transactions.atomic()` is never retried either (the whole transaction, not one statement, would need re-running). Only catches `DBConnectionError` — never `OperationalError` (e.g. a cancelled statement, where the same connection is still perfectly usable and retrying would be pointless). |
| `read_retry_backoff_base_seconds` | `0.1` | Backoff base for the above. |
| `min_size` / `max_size` | `1` / `16` | Connection pool bounds: whole numbers, `0 <= min_size <= max_size`, `1 <= max_size <= 10000`. A wrong type or range raises `ConfigurationError`. |
| `pool_acquire_timeout` | none (wait indefinitely) | Seconds to wait for a free pooled connection before `PoolTimeoutError`, a `DBConnectionError` (`0 < value <= 86400`, otherwise `ConfigurationError`). There is no "don't wait" value: `0` is rejected. Same validation and meaning on both drivers. |
| `command_timeout` | none (off) | Upper bound, in seconds, on a single command's round trip (`0 < value <= 86400`, otherwise `ConfigurationError`). Without it a query against a server that stopped answering without closing the connection (network partition, frozen process) waits forever. On expiry the call raises `TimeoutError` and the query is cancelled. Off by default so long migrations and bulk operations are unaffected. Enforced by hare itself on both drivers (asyncpg's native `command_timeout` never fires against a server that stopped answering, and `rust_pg` has no such option) around every query-executing call and bulk `COPY` — not around `begin`/`commit`/`rollback`/`savepoint` or `stream()`. See [Health checks and unresponsive servers](#health-checks). |

## <a id="rotating-credentials"></a>Rotating credentials (PostgreSQL)

A password that changes — a cloud IAM token, a secret store's dynamic credential — comes from a
function instead of the `password` setting. `password_provider` names it: a function, plain or
async, returning the password, or its dotted path in a DB_URL or a config file:

```python
async def get_database_password() -> str:
    return await secrets.get("orders-db")

await Hare.init(
    config={
        "connections": {
            "default": {
                "engine": "postgresql",
                "credentials": {
                    "host": "db.internal",
                    "user": "orders",
                    "database": "orders",
                    "password_provider": get_database_password,
                    "password_refresh_seconds": 300,
                },
            }
        },
        ...
    }
)
# or: postgresql://orders@db.internal/orders?password_provider=myapp.secrets.get_database_password
```

| Param | Default | Meaning |
|---|---|---|
| `password_provider` | none | The function giving the password. With `password` set too — `ConfigurationError`. It must return a non-empty string, otherwise `ConfigurationError`. |
| `password_refresh_seconds` | `600` | How long a password is used before the function is asked again (`0 < value <= 86400`, otherwise `ConfigurationError`). Without `password_provider` — `ConfigurationError`. |

The pool opens its new connections with the password the function gave last; the connections
already open stay. `asyncpg` asks for the password as it opens a connection, once it is older than
`password_refresh_seconds`; `rust_pg` opens its connections itself, so hare asks in the background
every `password_refresh_seconds` and hands the new password to the pool. When the server refuses a
password — it was rotated before the refresh — the function is asked right away and the statement
runs once more: the refused login happened before the statement reached the server, so this holds
for a write too. Inside a transaction its connection is already open, and nothing is repeated. A
dialect without password rotation (SQLite, ClickHouse) refuses `password_provider` as an unknown
setting.

## <a id="url-parameters-and-tls"></a>URL query parameters and TLS (PostgreSQL)

TLS is selected with `?sslmode=` (libpq values `disable`/`allow`/`prefer`/`require`/`verify-ca`/
`verify-full`), `?ssl=` (the same values, or a boolean: `true` means `require`, `false` means
`disable`) or `?ssl_mode=` — only one of them per URL. They map onto each driver's own setting:
`ssl_mode` for the default Rust driver (which has no `allow` mode — `UnSupportedError`), `ssl`
for asyncpg. A value outside these raises `ConfigurationError`.

With the Rust driver (`postgresql://`) every query parameter
must be one the driver understands: `min_size`/`max_size`,
`statement_cache_size`, `ssl_root_cert`, `application_name`, `schema`, `tenant_schema_template`, `tenant_row_level_security`, `host`, `port`, the
TLS parameters, `password_provider`/`password_refresh_seconds` and the parameters of the table above. Anything else (for example asyncpg's `max_queries`) raises
`ConfigurationError` instead of being dropped silently. With asyncpg
(`postgresql+asyncpg://`) a parameter must be one `asyncpg.connect()`/`create_pool()` accepts, and
asyncpg's own numeric parameters are range-checked: `max_queries` from `1`, `timeout` greater than
`0`, `max_inactive_connection_lifetime`/`max_cached_statement_lifetime` from `0` to `86400` seconds,
`max_cacheable_statement_size` from `0`. A value that doesn't fit its parameter's type or range
(`?min_size=abc`, `?max_queries=0`) raises `ConfigurationError` when the connection is configured,
not on the first query. A value given both in the address and as a query parameter
(`...@host:5432/db?port=6432`) raises `ConfigurationError` instead of one of them being dropped.

A failed connection keeps the driver's own reason in the message. A wrong password, an unknown
role or a missing database raises `ConfigurationError` on both drivers (no retry can fix it and
`connect_max_retries` doesn't retry it); an unreachable server raises `DBConnectionError`. With
`user`/`password`/`host`/`database` left unset, both drivers fall back to libpq's `PGUSER`/
`PGPASSWORD`/`PGHOST`/`PGDATABASE` environment variables (then the OS user and `localhost`).

Both PostgreSQL drivers run every session with `TimeZone=UTC`, whatever the server, database or role
default is — date/time casts, `CURRENT_DATE` and `Now()` defaults read the session zone, and hare
stores UTC instants. A `server_settings` `TimeZone` other than UTC (`UTC`, `Etc/UTC`, `GMT`, ... are
accepted) raises `ConfigurationError`; the zone your application sees values in is set by
`Hare.init(timezone=...)`.

Both PostgreSQL drivers return the same Python values from raw SQL: `numeric` as an exact
`Decimal` (any number of digits), `json`/`jsonb` as their text, `interval` as a `timedelta` (a
month counts as 30 days, a year as 365), `inet` as `ipaddress.ip_address`/`ip_interface`, `cidr` as
`ipaddress.ip_network`, `oid`/`xid` as `int`, the `reg*` types as names, and a `ROW(...)` as a
tuple. A `time` of `24:00:00`, which Python's `datetime.time` cannot hold, raises instead of reading
as midnight. With the Rust driver, a type it has no decoder for (`point`, `pg_lsn`, ...) is
returned as the text PostgreSQL prints for it; `timedelta` and `ipaddress` objects are accepted as
parameters. Raw SQL outside that list follows each driver: a range is asyncpg's `Range` or hare's
`hare.dialects.postgresql.fields.Range`, `money` is asyncpg's text (`'$1.00'`) or the Rust driver's
`Decimal`, and an infinite `timestamptz` is a naive or a UTC `datetime.max`/`min`. Model fields
(`RangeField`, `DatetimeField`, ...) decode every one of them the same on both drivers. A parameter of the wrong type for its column raises `OperationalError` rather than
being converted, and a database error keeps the server's `DETAIL`/`HINT` in its message and its
`sqlstate`, `constraint_name`, `table_name`, ... on the driver exception it was raised from.

The password may contain `@`, `/`, `?`, `#`, `[` and `]` literally (percent-encoding works too),
a query value may contain `@`, a percent-encoded database name is decoded (`/my%20db` → `my db`),
and a unix-socket directory can be given as `?host=/var/run/postgresql` with an empty host part or
percent-encoded as the host itself (`postgresql://user@%2Fvar%2Frun%2Fpostgresql/db`). An IPv6 zone id
is written percent-encoded too (`[fe80::1%25eth0]` → `fe80::1%eth0`).

## <a id="sqlite-connection-parameters"></a>SQLite connection parameters

A SQLite `db_url` path is percent-decoded like a PostgreSQL database name (`sqlite+aiosqlite://my%20db.sqlite`
→ `my db.sqlite`). Every other query parameter or `credentials` key sets a connection PRAGMA, and
only these are accepted, each value checked before anything is sent (a PRAGMA can't take a bind
parameter):

| Type | PRAGMAs | Values |
|---|---|---|
| keyword | `journal_mode`, `synchronous`, `temp_store`, `locking_mode`, `auto_vacuum`, `secure_delete` | the keywords SQLite documents for each (`journal_mode`: `DELETE`/`TRUNCATE`/`PERSIST`/`MEMORY`/`WAL`/`OFF`), case-insensitive |
| boolean | `foreign_keys`, `case_sensitive_like`, `recursive_triggers`, `automatic_index`, `cell_size_check`, `defer_foreign_keys`, `ignore_check_constraints`, `trusted_schema`, `reverse_unordered_selects` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` |
| whole number | `journal_size_limit` (`-1`..`2**40`), `cache_size`, `busy_timeout` (`0`..`86400000` ms), `mmap_size`, `page_size` (a power of two, `512`..`65536`), `wal_autocheckpoint`, `threads` (`0`..`64`), `max_page_count`, `cache_spill` | within the range |

Any other key (a typo such as `jurnal_mode`) or an unaccepted value raises `ConfigurationError`,
as does an `install_regexp_functions` value that isn't a boolean, and `automatic_index` turned on with
SQLite 3.38.0 to 3.41.0, whose automatic indexes ignore a comparison's collating sequence (these turn
it off by default — see [SQLite library faults](../dialects/dialects-and-features.md#sqlite-library-faults)).
A connection without `file_path` raises `ConfigurationError` too.

`timezone` is checked at `Hare.init()` — an unknown zone name raises `ConfigurationError` right
away instead of failing on the first datetime value.

## <a id="connections"></a>`Connections` / `ConnectionHandler`

```python
Connections.get(connection_alias: str) -> DatabaseClient
Connections.get_client(using: str | DatabaseClient | None) -> DatabaseClient | None
Connections.current() -> ConnectionHandler
Connections.aliases() -> list[str]
Connections.get_pool_statuses(connection_alias: str | None = None) -> list[PoolStatus]
Connections.create_task_outside_transactions(coroutine) -> asyncio.Task
await Connections.reconnect() -> None
```

`ConnectionHandler` (accessible as `context.connections` on a `HareContext`) is the actual owner of a
context's connections:

```python
.get(connection_alias) -> DatabaseClient           # creates on demand
.create_independent(connection_alias, credential_overrides=None) -> DatabaseClient   # used internally by autonomous()
.all() -> list[DatabaseClient]
.aliases() -> list[str]
async def close_all(discard: bool = True) -> None
```

`get_client(using)` is the connection a `using=` argument names — an alias's client, the client
itself, or `None`. `get_pool_statuses()` lists the open pools ([Pool metrics and health](../observability/pool-health.md)).
`create_task_outside_transactions(coroutine)` starts a task that sees no transaction of the caller —
every alias gives the shared client there.

`aliases()` returns every connection alias name configured for the current context (from
`Hare.init()`'s own connections config) — not resolved clients, just the names, e.g. for iterating
every configured connection in a health check.

`hare.core.constants.DEFAULT_CONNECTION_NAME` (`"default"`) is the alias `HareConfig.from_db_url(...)` gives
its one connection, the connection of an app config without `default_connection`, and the
connection `Transactions.atomic()` without `using` opens on when the apps'
default connections differ.

To find which connection a model's queries go to — after routers — use
[`Model.get_connection()`](../models/model-methods.md#classmethods) or `QuerySet.get_connection()`.

### <a id="reconnect"></a>Reconnecting after the database was replaced

```python
await Connections.reconnect()
```

Closes every connection and pool of the current context and keeps their configuration; the next
query opens new connections. Use it when the database was replaced under the running application:
a SQLite file swapped for a copy, a PostgreSQL database restored from a dump, or its connections
cut with `pg_terminate_backend()`.

- The connection objects and their names stay the same — code holding `Connections.get("default")`
  keeps working.
- Call it with no transaction open. A query started before the call finishes on the old
  connection.
- Each process reconnects its own connections; a worker pool calls it in every worker.
- Without an active context it raises `ConfigurationError`.

### <a id="health-checks"></a>Health checks and unresponsive servers

```python
ok: bool = await Connections.get("default").ping(timeout=5.0)
```

A whole health check — every connection pinged at once, its pools judged by criteria, the readiness route of a web application — is `HealthCheck` ([Pool metrics and health](../observability/pool-health.md)).

`ping()` runs `SELECT 1` and returns `True`/`False` instead of raising. It always bounds itself
with its own timeout (5 seconds by default, the `timeout=` argument), independent of any
connection setting: a server that stopped answering without closing the socket (network partition,
frozen process) makes no driver raise, so an unbounded call would hang forever. It returns `False`
on `DBConnectionError`, `OperationalError` and on timeout; anything else still propagates. Works
the same on every backend.

Ordinary queries have no such bound by default — a long migration or bulk operation must not be cut
off. To opt in on PostgreSQL, set `command_timeout` (seconds) in the credentials or as a DSN query
param (`?command_timeout=30`); see
[connection resilience](#connection-resilience).
A command that outlives it is cancelled and raises `TimeoutError` (on both drivers), and the
connection stays usable. hare enforces it itself around each query-executing call (and bulk `COPY`)
rather than through a driver option: `asyncpg`'s native `command_timeout` only raises after its
cancel request round-trips, which never happens against a server that stopped answering, and
`rust_pg` has no such option at all. Transaction control (`begin`/`commit`/`rollback`/`savepoint`)
is deliberately left unbounded, since cancelling one of those mid-flight would leave the
transaction state unknown.

On `rust_pg`, a statement of a transaction cancelled mid-flight sends PostgreSQL a cancel request over
a separate connection; the transaction's next statement waits until that request has been
delivered, so a request arriving late can never cancel the next statement instead.
