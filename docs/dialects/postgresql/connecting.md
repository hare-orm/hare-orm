# PostgreSQL: connecting

How hare connects to PostgreSQL: the two drivers, the URL and its TLS parameters, credentials that
rotate, how a connection survives a server that restarts or drops it, and what each driver does with the
values of a raw statement.

## <a id="choosing-a-postgres-driver"></a>The drivers

The native driver's raw parameter conversion tracks current ancestors across lists,
JSON containers and range bounds. Circular references raise `ValueError`; reusing a
child value in separate branches is allowed. At most 256 nested containers or ranges
are traversed in one parameter; deeper input raises `RecursionError` before sending
SQL. This protects native stack usage independently of ORM field validation.

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
`make build_native` command that builds it; `postgresql+asyncpg` needs no extension.

### <a id="rust-pg-threads"></a>The worker threads of `rust_pg`

`rust_pg` runs its connections on up to four worker threads of its own, no more than the machine's cores:
the queries are started by the one thread running the event loop, and a few workers keep up with it —
while one decodes a large result, the others still serve the other connections; many more would wait for
work on cores the event loop and the database need. Set the number with tokio's own environment variable
before hare is imported:

```bash
TOKIO_WORKER_THREADS=4 python app.py
```

The native driver uses `pyo3-async-runtimes` to bridge Rust futures to the caller's event loop,
preserving task context. Cancelling the Python future drops the Rust operation; a separate database
guard cancels the SQL and protects the connection while cancellation is delivered. Close clients
before closing their event loop. Forcibly closing a loop with pending native work prevents delivery
of its result: the bridge reports `RuntimeError: Event loop is closed`, and Python may retain that
diagnostic's traceback in `sys.last_exc`. This is not a successful cancellation of the database work.

## <a id="connection-resilience"></a>Connection parameters

Both drivers accept these as URL query parameters (`?connect_max_retries=3&...`) or `credentials` dict
keys, the same way `max_size` is passed. Numeric URL text is converted to integers or seconds.
Typed settings pass through unchanged; applicable driver and library constraints are enforced by
their consumers. Unparseable numeric text raises `ConfigurationError` when configured:

| Parameter | Default | Meaning |
|---|---|---|
| `connect_max_retries` | `0` | Retries for the *initial* pool connection, exponential backoff (`connect_retry_backoff_base_seconds * 2**attempt`). `0` (the default) means the first attempt's exception propagates unchanged. |
| `connect_retry_backoff_base_seconds` | `0.1` | Backoff base for the above. |
| `port` | `5432` | The server TCP port, parsed as an integer from URL text. |
| `statement_cache_size` | driver default | The driver statement-cache capacity — see [`statement_cache_size`](#statement-cache-size). |
| `transaction_pooling` | `false` | The connection goes through a pooler in transaction pooling (PgBouncer) — see [Connection poolers](../../connections/connection-poolers.md). |
| `direct_host` / `direct_port` | none / `port` | The server's own address past the pooler, for LISTEN and session settings. Only with `transaction_pooling`. |
| `read_retry_max_retries` | `0` | Retries a query lost to a connection dropping **mid-query** (RDS failover, a network blip), not just at connect time — same exponential backoff shape, its own independent retry budget — see [`read_retry_max_retries`](#read-retry-max-retries) |
| `read_retry_backoff_base_seconds` | `0.1` | Backoff base for the above. |
| `min_size` / `max_size` | `1` / `16` | Minimum and maximum pool sizes; the pool implementation checks their constraints. |
| `max_inactive_connection_lifetime` | `300` | Seconds a pooled connection may stay unused before the pool closes it, including `min_size` connections; they reopen when needed. `0` keeps them open. |
| `pool_acquire_timeout` | none (wait indefinitely) | Seconds to wait for a free connection before `PoolTimeoutError`, a `DBConnectionError`. The pool implementation controls timeout scheduling and accepted values. |
| `command_timeout` | none (off) | Seconds allowed for one command round trip, enforced through `asyncio.timeout()` — see [`command_timeout`](#command-timeout). |

### <a id="statement-cache-size"></a>`statement_cache_size`

A driver statement-cache capacity; hare does not impose its own upper limit. URL text is converted to
an integer, and the driver interprets the setting. `0` turns the cache off: each statement is prepared
for its own run
— on `rust_pg` a statement whose values all carry a type of their own (a UUID, a date, a time, an aware
`datetime`, a `timedelta`, a `Decimal`, a `float`, `bytes`, a `bool`, `None`) goes out with them in one
round trip instead.

The native driver invalidates a stale cached plan when PostgreSQL reports a changed result type,
then raises the error. It does not silently replay that statement: the same server error can originate
in a nested query after other effects have occurred. A later explicit call prepares a fresh plan.
Schema-editor operations explicitly invalidate driver statement and type metadata after DDL, including
partially executed migration scripts. Native connections refresh their caches before their next use;
asyncpg uses its public schema reload. Application-managed raw DDL can request this boundary with
`await connection.invalidate_schema_cache()`.

For raw `execute()` and `execute_described()`, column names and affected-row counts come from
the driver's statement metadata and PostgreSQL's command completion. Comments, quoted text and
CTEs are not inspected to guess whether a command writes rows or contains `RETURNING`.
An empty result can still describe its columns. A count is `None` when no completion was reported.
Connection errors retain the original driver's error and cause chain; reporting an error does not
perform extra DNS lookups or authentication attempts.

### <a id="read-retry-max-retries"></a>`read_retry_max_retries`

Retries a query lost to a connection dropping **mid-query** (RDS failover, a network blip), not just at
connect time — same exponential backoff shape, its own independent retry budget. Only ever applies to a
statement explicitly marked as a safe read (`execute(..., retry_read=True)`) run **outside** an explicit
transaction — a
write is never retried blind (it might have already landed), and a query inside `Transactions.atomic()`
is never retried either (the whole transaction, not one statement, would need re-running). Only catches
`DBConnectionError` — never `OperationalError` (e.g. a cancelled statement, where the same connection is
still perfectly usable and retrying would be pointless).

ORM read queries pass this permission to their SQL invocation; it is not inherited by nested calls
or observers. Direct raw SQL defaults to no retry. Its author must establish safety before opting in:
returning rows or starting with `SELECT` does not rule out side effects. Each attempt reports its
duration separately, excluding backoff; sensitive values remain hidden in failed-attempt events.

### <a id="command-timeout"></a>`command_timeout`

Upper bound, in seconds, on a single command's round trip. URL text is converted to a number;
typed settings pass to `asyncio.timeout()` without a separate positive-value check in hare.
`None` disables the deadline; zero or a negative value schedules cancellation at the next event-loop
iteration. Without a deadline, a query against a server that stopped answering without closing the
connection (network partition, frozen process) waits forever. On expiry the call raises `TimeoutError`
and the query is cancelled. Off by default so long migrations and bulk operations are unaffected.
Enforced by hare itself on both drivers (asyncpg's native `command_timeout` never fires against a server
that stopped answering, and `rust_pg` has no such option) around every query-executing call and bulk
`COPY` — not around `begin`/`commit`/`rollback`/`savepoint` or `stream()`. Transaction control has
separate lifecycle limits, including bounded cleanup after cancellation; these are not governed by
`command_timeout`. Before reusing a cancelled command's connection, hare waits for safe completion.
If completion cannot be established, the connection is discarded instead of reused.

On `rust_pg`, a statement of a transaction cancelled mid-flight sends PostgreSQL a cancel request over a
separate connection; the transaction's next statement waits until that request has been delivered, so a
request arriving late can never cancel the next statement instead.

The cancelled query stops on the server as well, also behind a pooler such as PgBouncer: a pooler
forwards a cancel request only while the client connection it names is still open, so a connection taken
out of the pool for a timed-out query is closed only after its cancel request has been delivered — or,
when no answer to it comes within another `command_timeout` (a network that stopped answering), closed
then, so the call still ends after about twice `command_timeout`. See
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
| `password_refresh_seconds` | `600` | How long a password is used before the function is asked again (`value > 0`, otherwise `ConfigurationError`). Without `password_provider` — `ConfigurationError`. |

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

`?ssl_root_cert=` names the file of the certificates the server's certificate is checked against, on both
drivers (asyncpg with a TLS mode — an `ssl=` `SSLContext` holds its certificates itself, and the two
together raise `ConfigurationError`). A file that can't be read or holds no certificate raises
`ConfigurationError` before a connection is opened.
`rust_pg` also reports TLS setup failures as `ConfigurationError`, including verification requested
on a system with no usable root certificates; these failures are not retried as network errors.

With `rust_pg` (`postgresql://`) every query parameter must be one the driver understands:
`min_size`/`max_size`, `max_inactive_connection_lifetime`, `statement_cache_size`, `ssl_root_cert`,
`application_name`, `schema`, `tenant_schema_template`, `tenant_row_level_security`, `host`, `port`, the
TLS parameters, `password_provider`/`password_refresh_seconds` and the parameters of the table above.
Anything else (for example asyncpg's `max_queries`) raises `ConfigurationError` instead of being dropped
silently. With asyncpg (`postgresql+asyncpg://`) a parameter must be `ssl_root_cert` or one
`asyncpg.connect()`/`create_pool()` accepts. Known numeric URL settings are parsed; typed values
retain their representation and are passed to asyncpg. Invalid numeric text such as `?min_size=abc`
raises `ConfigurationError` during configuration; driver-specific constraints are checked when used.
A value given both in the address and as a query parameter
(`...@host:5432/db?port=6432`) raises `ConfigurationError` instead of one of them being dropped.

A failed connection keeps the driver's own reason in the message. A wrong password, an unknown role or a
missing database raises `ConfigurationError` on both drivers (no retry can fix it and
`connect_max_retries` doesn't retry it); an unreachable server and a server out of connections, memory
or disk (SQLSTATE class 53, "too many clients") raise `DBConnectionError`, which `connect_max_retries`
retries. A pool that fails to open closes the connections it has opened. With
`user`/`password`/`host`/`database` left unset, both drivers fall back to libpq's `PGUSER`/
`PGPASSWORD`/`PGHOST`/`PGDATABASE` environment variables (then the OS user and `localhost`).

Percent-encode reserved delimiters in usernames and passwords: `@`, `:`, `/`, `?`, `#`, `[` and `]`.
For example, `p@s?a=b` becomes `p%40s%3Fa%3Db`; Python's `urllib.parse.quote(password, safe="")`
encodes a complete credential. Components are decoded once: `%252F` means the literal text `%2F`.
Separate `credentials` fields accept the original username and password without URL encoding.
An explicit empty password or query value stays empty; an omitted credential can use the driver's
environment default. URLs have no fragment, and malformed URLs are hidden completely in configuration
representations. A query value may contain `@`, a percent-encoded database name is decoded
(`/my%20db` → `my db`), and a
unix-socket directory can be given as `?host=/var/run/postgresql` with an empty host part or
percent-encoded as the host itself (`postgresql://user@%2Fvar%2Frun%2Fpostgresql/db`). An IPv6 zone id is
written percent-encoded too (`[fe80::1%25eth0]` → `fe80::1%eth0`).
DNS host names are normalized to lowercase; socket paths and IPv6 zone identifiers retain their case.

## <a id="search-path"></a>Schema and search path

`schema` names one exact shared schema: `schema="Team,Archive"` is one name containing a comma.
For an ordered search path use `search_path=SearchPath("Team,Archive", "public")`, importing
`SearchPath` from `hare.dialects.postgresql`. A sequence of exact names is also accepted. In a URL,
`search_path` uses PostgreSQL setting syntax, percent-encoded as a query value. In unquoted names,
ASCII letters fold to lowercase, while non-ASCII letters retain their case as in a UTF-8 database.
Punctuation is allowed; commas and PostgreSQL scanner whitespace separate names. Quoted names
preserve case, commas and doubled quotes. Empty names are rejected; identifier length is left to
the server. `$user` and `pg_temp` retain their PostgreSQL meaning. `SearchPath()` sets an empty path.

Without `search_path`, `schema` supplies the path's single entry; without either option, the server
chooses its default. `server_settings["search_path"]` accepts the same setting syntax; do not also
set the separate `search_path` option. Tenant connections prepend the tenant schema to the configured
path and keep `schema` unchanged for shared-table qualification. The same encoding is used by both
drivers, transaction-pooler checks, shadow schemas and `dbshell`.

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
through hare's binary codecs, installed when a connection opens and refreshed at explicit schema
boundaries before its next ORM statement, including on a connection held by a transaction. Commit,
rollback and savepoint rollback invalidate catalog snapshots again when visibility changes.
Removing a registered type retires affected pool connections after release; ordinary DDL and adding
extensions do not require reconnecting. asyncpg's own text decoding crashes on an empty value in an
array of these extension types. After schema changes outside hare's schema editor, call
`await connection.invalidate_schema_cache()` before using those types. A `time` of `24:00:00`, which
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
- rust_pg decodes binary values before returning the result; conversion failures are reported by
  the executing call. Repeated row access uses the decoded values and creates Python objects on demand.
- asyncpg writes a `Decimal` of more than 131072 digits before its point as `0`, a limit of its codec;
  rust_pg refuses it. A filter by a `DecimalField` compares such a number as the first one past the
  column's range, so it never reaches the driver.
- A login with no password raises `ConfigurationError`, like a wrong one, on both — rust_pg refuses it
  before the server asks.

The native driver selects decoders from physical column types and decodes each binary value once
before publishing the result. An unsupported non-NULL value requests the server's text representation;
the returned text fills that value without decoding the row again. SQL NULL never requests a text
conversion. A malformed value of a supported type raises a conversion error instead of falling back
to server text. Repeated result access reuses the decoded values; Python objects are created on demand.

The native driver checks binary container lengths, counts and trailing bytes at every nested
value. Arrays, records and range bounds share one decoding context. The resulting nested
container tree is limited to 256 levels to bound native stack use; array dimensions count
toward that limit. TSQUERY uses a flat representation, so the number of its operators does
not consume that nesting budget. Python parameters use separate ancestor tracking: cyclic
lists, dictionaries and ranges are rejected, while shared acyclic children remain valid.
The native Python-input nesting limit is also 256 levels.
