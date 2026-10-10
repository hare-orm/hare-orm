# Connection poolers

A pooler in **transaction pooling** — PgBouncer with `pool_mode = transaction` — lends a server
connection to a client for one transaction at a time. Between two transactions (and between two
statements outside a transaction) the next statement may run on another server connection. hare's
queries and transactions work behind it unchanged; what holds a session of its own needs the
settings below.

hare supports **PgBouncer 1.21 and newer** with prepared statements across transactions
(`max_prepared_statements > 0`) — both drivers keep their statement cache behind it.

## <a id="setting-up-hare"></a>Setting up hare

Tell hare that the connection goes through such a pooler, and where the server itself is:

```text
postgresql://app:secret@pgbouncer:6432/app?transaction_pooling=true&direct_host=postgres&direct_port=5432
```

| Param | Default | Meaning |
|---|---|---|
| `transaction_pooling` | `false` | The connection goes through a pooler in transaction pooling. |
| `direct_host` | none | The server's own host, past the pooler — for the work holding a session of its own (below). Only with `transaction_pooling`, otherwise `ConfigurationError`. |
| `direct_port` | the connection's `port` | The server's own port. Needs `direct_host`. |

The user, password, database and TLS settings of the direct connection are the connection's own.

## <a id="what-hare-does"></a>What hare does differently

With `transaction_pooling=true`:

- **`LISTEN`** — `NotificationListener`, the `ListenNotifyWakeup` of the
  [transactional outbox](../integrations/outbox.md) — opens its connection on `direct_host`. A
  `LISTEN` through the pooler would lose its notifications as soon as its transaction ends.
  Without `direct_host` it raises `ConfigurationError`.
- **The session lock timeout of a non-atomic migration** (`lock_timeout`) is set on a connection to
  `direct_host`, so it never stays behind on a server connection the pooler lends to another
  client. Without `direct_host` — `ConfigurationError`.
- **A statement whose prepared plan a schema change made stale.** PgBouncer shares a prepared
  statement between its clients by the statement's text, so after a migration that changes a
  statement's result (`ALTER COLUMN ... TYPE`) the server refuses it with *cached plan must not
  change result type* on every server connection that prepared it before. Outside a transaction
  hare runs such a statement again under another text (`/* hare: replanned 1 */` appended), which
  PgBouncer prepares afresh, and keeps that text for the statement's later runs. Inside a
  transaction the statement fails as it does without a pooler — the transaction runs again.
- **`DROP DATABASE`** drops the database `WITH (FORCE)` — the pooler keeps its sessions on the
  database open after its clients leave.
- **The search path** of the `schema` option and of a
  [schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md) is checked once when the
  pool opens. A pooler that doesn't pass it on to the server would run every tenant's statements in
  one schema; hare refuses to start with `ConfigurationError` instead.
- **A login the pooler refuses** (a wrong password) raises `ConfigurationError`, as the server's own
  refusal does, and is never retried.

## <a id="what-needs-nothing"></a>What needs nothing

These hold no session beyond their transaction and work behind the pooler as they are:
transactions and savepoints, the tenants of [row level security](../soft-delete-versions-tenants/multi-tenancy.md)
(set per transaction), `statement_timeout`/`lock_timeout` of `Transactions.atomic()` (`SET LOCAL`),
the migration lock (a transaction-level advisory lock), two-phase commit, `stream()` inside a
transaction, `select_for_update()`, `COPY`, and the outbox relay.

A setting changed for the database or a role (`ALTER DATABASE ... SET`) reaches only the server
connections the pooler opens after it — the open ones keep the old value until PgBouncer reconnects
them (`RECONNECT` in its admin console).

## <a id="pgbouncer-settings"></a>PgBouncer settings

```ini
[pgbouncer]
pool_mode = transaction
; Prepared statements across transactions - required by hare.
max_prepared_statements = 200
; Needed with the schema option or a schema per tenant.
track_extra_parameters = search_path
```

PgBouncer passes `TimeZone` and `application_name` on by itself — hare's session time zone (UTC)
needs nothing.

## <a id="health-checks"></a>Health checks

A health check's ping goes through the pooler. `HealthCheck(check_direct=True)` pings the server
itself as well, by `direct_host`/`direct_port` — a pooler up while the server is down shows
([Pool metrics and health](../observability/pool-health.md#behind-pgbouncer)). The pool of that
direct client has the role `DIRECT` in the pool status and metrics.
