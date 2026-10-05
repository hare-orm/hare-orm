# PostgreSQL: connecting

How hare connects to PostgreSQL: the two drivers, the URL and its TLS parameters, credentials that
rotate, how a connection survives a server that restarts or drops it, and what each driver does with the
values of a raw statement.

## <a id="choosing-a-postgres-driver"></a>The drivers

A connection picks its driver with `engine` — the same word as the scheme of its database URL:

| `engine` / database URL scheme | Driver |
|---|---|
| `postgresql` | `rust_pg`, hare's driver written in Rust — installed with hare |
| `postgresql+asyncpg` | `asyncpg` (install `hare-orm[asyncpg]`) |

A driver's scheme is `<dialect>+<driver>`; the plain `postgresql://` is `rust_pg`. The schemes of every
database are in [Dialects and features](../dialects-and-features.md).

```python
connections = {
    "reporting": {"engine": "postgresql+asyncpg", "credentials": {"host": "db.internal", "database": "reporting"}},
    "analytics": "postgresql://user:pass@host:5432/db",
}
```

If the Rust extension is missing — the pure wheel on a platform without a prebuilt wheel, or a source
checkout that hasn't been built — a `postgresql` connection raises `ConfigurationError` naming the
`maturin develop` command that builds it; `postgresql+asyncpg` needs no extension.

### <a id="rust-pg-threads"></a>The worker threads of `rust_pg`

`rust_pg` runs its connections on up to four worker threads of its own, no more than the machine's cores:
the queries are started by the one thread running the event loop, and a few workers keep up with it —
while one decodes a large result, the others still serve the other connections; many more would wait for
work on cores the event loop and the database need. Set the number with tokio's own environment variable
before hare is imported:

```bash
TOKIO_WORKER_THREADS=4 python app.py
```

## <a id="connection-resilience"></a>Connection parameters

Both drivers accept these as URL query parameters (`?connect_max_retries=3&...`) or `credentials` dict
keys, the same way `max_size` is passed. Retry counts are whole numbers from `0` to `100`, backoff bases
finite numbers of seconds from `0` to `3600`; anything else (`"inf"`, `"nan"`, a negative value) raises
`ConfigurationError` at configuration time:

| Parameter | Default | Meaning |
|---|---|---|
| `connect_max_retries` | `0` | Retries for the *initial* pool connection, exponential backoff (`connect_retry_backoff_base_seconds * 2**attempt`). `0` (the default) means the first attempt's exception propagates unchanged. |
| `connect_retry_backoff_base_seconds` | `0.1` | Backoff base for the above. |
| `port` | `5432` | A whole number from `1` to `65535`, otherwise `ConfigurationError`. |
| `statement_cache_size` | driver default | A whole number from `0` to `1000000` — see [`statement_cache_size`](#statement-cache-size) |
| `transaction_pooling` | `false` | The connection goes through a pooler in transaction pooling (PgBouncer) — see [Connection poolers](../../connections/connection-poolers.md). |
| `direct_host` / `direct_port` | none / `port` | The server's own address past the pooler, for LISTEN and session settings. Only with `transaction_pooling`. |
| `read_retry_max_retries` | `0` | Retries a query lost to a connection dropping **mid-query** (RDS failover, a network blip), not just at connect time — same exponential backoff shape, its own independent retry budget — see [`read_retry_max_retries`](#read-retry-max-retries) |
| `read_retry_backoff_base_seconds` | `0.1` | Backoff base for the above. |
| `min_size` / `max_size` | `1` / `16` | Connection pool bounds: whole numbers, `0 <= min_size <= max_size`, `1 <= max_size <= 10000`. A wrong type or range raises `ConfigurationError`. |
| `max_inactive_connection_lifetime` | `300` | Seconds a pooled connection may stay unused before the pool closes it — `min_size` ones too, reopened when needed; `0` keeps them open (`0 <= value <= 86400`, otherwise `ConfigurationError`). Same meaning on both drivers. |
| `pool_acquire_timeout` | none (wait indefinitely) | Seconds to wait for a free pooled connection before `PoolTimeoutError`, a `DBConnectionError` (`0 < value <= 86400`, otherwise `ConfigurationError`). There is no "don't wait" value: `0` is rejected. Same validation and meaning on both drivers. |
| `command_timeout` | none (off) | Upper bound, in seconds, on a single command's round trip (`0 < value <= 86400`, otherwise `ConfigurationError`) — see [`command_timeout`](#command-timeout) |

### <a id="statement-cache-size"></a>`statement_cache_size`

A whole number from `0` to `1000000`. `0` turns the cache off: each statement is prepared for its own run
— on `rust_pg` a statement whose values all carry a type of their own (a UUID, a date, a time, an aware
`datetime`, a `timedelta`, a `Decimal`, a `float`, `bytes`, a `bool`, `None`) goes out with them in one
round trip instead.

### <a id="read-retry-max-retries"></a>`read_retry_max_retries`

Retries a query lost to a connection dropping **mid-query** (RDS failover, a network blip), not just at
connect time — same exponential backoff shape, its own independent retry budget. Only ever applies to a
statically read-only query (`AwaitableQuery.is_read_only`) run **outside** an explicit transaction — a
write is never retried blind (it might have already landed), and a query inside `Transactions.atomic()`
is never retried either (the whole transaction, not one statement, would need re-running). Only catches
`DBConnectionError` — never `OperationalError` (e.g. a cancelled statement, where the same connection is
still perfectly usable and retrying would be pointless).

### <a id="command-timeout"></a>`command_timeout`

Upper bound, in seconds, on a single command's round trip (`0 < value <= 86400`, otherwise
`ConfigurationError`). Without it a query against a server that stopped answering without closing the
connection (network partition, frozen process) waits forever. On expiry the call raises `TimeoutError`
and the query is cancelled. Off by default so long migrations and bulk operations are unaffected.
Enforced by hare itself on both drivers (asyncpg's native `command_timeout` never fires against a server
that stopped answering, and `rust_pg` has no such option) around every query-executing call and bulk
`COPY` — not around `begin`/`commit`/`rollback`/`savepoint` or `stream()`. Transaction control is left
unbounded since cancelling one of those mid-flight would leave the transaction state unknown. The
connection of a cancelled command stays usable.

On `rust_pg`, a statement of a transaction cancelled mid-flight sends PostgreSQL a cancel request over a
separate connection; the transaction's next statement waits until that request has been delivered, so a
request arriving late can never cancel the next statement instead.

The cancelled query stops on the server as well, also behind a pooler such as PgBouncer: a pooler
forwards a cancel request only while the client connection it names is still open, so a connection taken
out of the pool for a timed-out query is closed only after its cancel request has been delivered. See
[Health checks and unresponsive servers](../../connections/connections.md#health-checks).

## <a id="rotating-credentials"></a>Rotating credentials

A password that changes — a cloud IAM token, a secret store's dynamic credential — comes from a function
instead of the `password` setting. `password_provider` names it: a function, plain or async, returning
the password, or its dotted path in a database URL or a config file:

```python
async def get_database_password() -> str:
    return await secrets.get("library-db")


await Hare.init(
    config={
        **HARE_CONFIG,
        "connections": {
            "default": {
                "engine": "postgresql",
                "credentials": {
                    "host": "db.internal",
                    "user": "library",
                    "database": "library",
                    "password_provider": get_database_password,
                    "password_refresh_seconds": 300,
                },
            }
        },
    }
)
# or: postgresql://library@db.internal/library?password_provider=library.secrets.get_database_password
```

| Parameter | Default | Meaning |
|---|---|---|
| `password_provider` | none | The function giving the password. With `password` set too — `ConfigurationError`. It must return a non-empty string, otherwise `ConfigurationError`. |
| `password_refresh_seconds` | `600` | How long a password is used before the function is asked again (`0 < value <= 86400`, otherwise `ConfigurationError`). Without `password_provider` — `ConfigurationError`. |

The pool opens its new connections with the password the function gave last; the connections already open
stay. `asyncpg` asks for the password as it opens a connection, once it is older than
`password_refresh_seconds`; `rust_pg` opens its connections itself, so hare asks in the background every
`password_refresh_seconds` and hands the new password to the pool. A function failing to give a new
password leaves the last one in use on both drivers — logged as a warning; with no password yet, or after
the server refused it, its error is raised. When the server refuses a password — it was rotated before
the refresh — the function is asked right away and the statement runs once more: the refused login
happened before the statement reached the server, so this holds for a write too. A host naming several
addresses (`localhost` — `127.0.0.1` and `::1`) reports the refusal of the address that answered, not the
connection refused on another. Inside a transaction its connection is already open, and nothing is
repeated. A dialect without password rotation (SQLite, ClickHouse) refuses `password_provider` as an
unknown setting.

## <a id="url-parameters-and-tls"></a>URL query parameters and TLS

TLS is selected with `?sslmode=` (libpq values `disable`/`allow`/`prefer`/`require`/`verify-ca`/
`verify-full`), `?ssl=` (the same values, or a boolean: `true` means `require`, `false` means `disable`)
or `?ssl_mode=` — only one of them per URL. They map onto each driver's own setting: `ssl_mode` for
`rust_pg` (which has no `allow` mode — `UnSupportedError`), `ssl` for asyncpg. A value outside these
raises `ConfigurationError`.

With `rust_pg` (`postgresql://`) every query parameter must be one the driver understands:
`min_size`/`max_size`, `max_inactive_connection_lifetime`, `statement_cache_size`, `ssl_root_cert`,
`application_name`, `schema`, `tenant_schema_template`, `tenant_row_level_security`, `host`, `port`, the
TLS parameters, `password_provider`/`password_refresh_seconds` and the parameters of the table above.
Anything else (for example asyncpg's `max_queries`) raises `ConfigurationError` instead of being dropped
silently. With asyncpg (`postgresql+asyncpg://`) a parameter must be one
`asyncpg.connect()`/`create_pool()` accepts, and asyncpg's own numeric parameters are range-checked:
`max_queries` from `1`, `timeout` greater than `0`,
`max_inactive_connection_lifetime`/`max_cached_statement_lifetime` from `0` to `86400` seconds,
`max_cacheable_statement_size` from `0`. A value that doesn't fit its parameter's type or range
(`?min_size=abc`, `?max_queries=0`) raises `ConfigurationError` when the connection is configured, not on
the first query. A value given both in the address and as a query parameter
(`...@host:5432/db?port=6432`) raises `ConfigurationError` instead of one of them being dropped.

A failed connection keeps the driver's own reason in the message. A wrong password, an unknown role or a
missing database raises `ConfigurationError` on both drivers (no retry can fix it and
`connect_max_retries` doesn't retry it); an unreachable server raises `DBConnectionError`. With
`user`/`password`/`host`/`database` left unset, both drivers fall back to libpq's `PGUSER`/
`PGPASSWORD`/`PGHOST`/`PGDATABASE` environment variables (then the OS user and `localhost`).

The password may contain `@`, `/`, `?`, `#`, `[` and `]` literally (percent-encoding works too), a query
value may contain `@`, a percent-encoded database name is decoded (`/my%20db` → `my db`), and a
unix-socket directory can be given as `?host=/var/run/postgresql` with an empty host part or
percent-encoded as the host itself (`postgresql://user@%2Fvar%2Frun%2Fpostgresql/db`). An IPv6 zone id is
written percent-encoded too (`[fe80::1%25eth0]` → `fe80::1%eth0`).

## <a id="session-time-zone"></a>The session time zone

Both drivers run every session with `TimeZone=UTC`, whatever the server, database or role default is —
date/time casts, `CURRENT_DATE` and `Now()` defaults read the session zone, and hare stores UTC instants.
A `server_settings` `TimeZone` other than UTC (`UTC`, `Etc/UTC`, `GMT`, ... are accepted) raises
`ConfigurationError`; the zone your application sees values in is set by `Hare.init(timezone=...)`.

## <a id="driver-differences"></a>Raw SQL on the two drivers

Both drivers return the same Python values from raw SQL: `numeric` as an exact `Decimal` (any number of
digits), `json`/`jsonb` as their text, `interval` as a `timedelta` (a month counts as 30 days, a year as
365), `inet` as `ipaddress.ip_address`/`ip_interface`, `cidr` as `ipaddress.ip_network`, `oid`/`xid` as
`int`, the `reg*` types as names, `tsvector`, `citext`, `ltree` and `hstore` (in an array too, an empty
one among them) as the text PostgreSQL prints, and a `ROW(...)` as a tuple. asyncpg reads these four
through hare's binary codecs, which a connection gets as it opens and again after hare runs a script
creating or dropping an extension; asyncpg's own text decoding of them crashes the process on an empty
value in an array. A pool opened before an extension was created outside hare reads its types that way
until its connections are opened again (`await Connections.reconnect()`). A `time` of `24:00:00`, which
Python's `datetime.time` cannot hold, raises instead of reading as midnight. With `rust_pg`, a type it
has no decoder for (`point`, `pg_lsn`, ...) is returned as the text PostgreSQL prints for it; `timedelta`
and `ipaddress` objects are accepted as parameters. Raw SQL outside that list follows each driver: a
range is asyncpg's `Range` or hare's `hare.dialects.postgresql.fields.Range`, `money` is asyncpg's text
(`'$1.00'`) or `rust_pg`'s `Decimal`, and an infinite `timestamptz` is a naive or a UTC
`datetime.max`/`min`. Model fields (`RangeField`, `DatetimeField`, ...) decode every one of them the same
on both drivers. A parameter of the wrong type for its column raises `OperationalError` rather than being
converted, and a database error keeps the server's `DETAIL`/`HINT` in its message and its `sqlstate`,
`constraint_name`, `table_name`, ... on the driver exception it was raised from.

A raw statement's values pass to the driver as they are, so what asyncpg and rust_pg do with them shows
through. hare makes them agree where it can:

- **A wrong number of parameters** raises `OperationalError` on both — asyncpg refuses it before sending,
  the server refuses it for rust_pg; the connection stays usable.
- **A value Python's type holds none of** — `interval 'infinity'` or one of millions of years,
  `'24:00:00'::time`, a date before year 1 or after 9999 — raises `OperationalError` from a query of
  hare. An `interval` is read the way asyncpg reads it on both: a year of 365 days, a month of 30.
- rust_pg decodes a row when it is read: the rows `connection.execute(...)` returns raise the driver's
  own `rust.native.pg.ConversionError` for such a value when the value is read, outside the call.
- asyncpg writes a `Decimal` of more than 131072 digits before its point as `0`, a limit of its codec;
  rust_pg refuses it. A filter by a `DecimalField` compares such a number as the first one past the
  column's range, so it never reaches the driver.
- A login with no password raises `ConfigurationError`, like a wrong one, on both — rust_pg refuses it
  before the server asks.
