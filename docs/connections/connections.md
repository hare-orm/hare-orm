# Connections

`Connections` holds the open connections of a context: a connection by its alias, reconnecting after the
database was replaced, checking that a server answers. How to connect to each database — drivers, URL
parameters, TLS — is on that database's page: [PostgreSQL](../dialects/postgresql/connecting.md),
[SQLite](../dialects/sqlite/connecting.md), [ClickHouse](../dialects/clickhouse/connecting.md).

## <a id="connections"></a>`Connections` / `ConnectionHandler`

```python
class Connections:
    @staticmethod
    def get(connection_alias: str) -> DatabaseClient: ...

    @staticmethod
    def get_client(using: str | DatabaseClient | None) -> DatabaseClient | None: ...

    @staticmethod
    def current() -> ConnectionHandler: ...

    @staticmethod
    def aliases() -> list[str]: ...

    @staticmethod
    def get_pool_statuses(connection_alias: str | None = None) -> list[PoolStatus]: ...

    @staticmethod
    def create_task_outside_transactions(
        coroutine: Coroutine[Any, Any, TaskResult],
    ) -> asyncio.Task[TaskResult]: ...

    @staticmethod
    async def reconnect() -> None: ...
```

`ConnectionHandler` (accessible as `context.connections` on a `HareContext`) is the actual owner of a
context's connections:

```python
class ConnectionHandler:
    # creates the client on first use
    def get(self, connection_alias: str) -> DatabaseClient: ...

    # what Transactions.autonomous() runs on
    def create_independent(
        self,
        connection_alias: str,
        credential_overrides: Mapping[str, Any] | None = None,
    ) -> DatabaseClient: ...

    def all(self) -> list[DatabaseClient]: ...

    def aliases(self) -> list[str]: ...

    async def close_all(self, discard: bool = True) -> None: ...
```

`get_client(using)` is the connection a `using=` argument names — an alias's client, the client itself,
or `None`. `get_pool_statuses()` lists the open pools ([Pool metrics and
health](../observability/pool-health.md)). `create_task_outside_transactions(coroutine)` starts a task
that sees no transaction of the caller — every alias gives the shared client there.

`close_all()` closes every connection of the context — how long it waits for the queries still running is
in [Closing connections](configuration.md#closing-connections). With `discard=True` (the default;
`Hare.close_connections()` closes this way) the connection objects are dropped too: a task still holding
one gets `DBConnectionError` on its next query instead of opening a new pool nothing would close. With
`discard=False` the objects stay and open new connections on the next query — what
[`Connections.reconnect()`](#reconnect) does.

`aliases()` returns every connection alias name configured for the current context (the connections of
the `Hare.init()` configuration) — not resolved clients, just the names, e.g. for iterating every
configured connection in a health check.

`hare.core.constants.DEFAULT_CONNECTION_NAME` (`"default"`) is the alias `HareConfig.from_db_url(...)`
gives its one connection, the connection of an app config without `default_connection`, and the
connection `Transactions.atomic()` without `using` opens on when the apps' default connections differ.

To find which connection a model's queries go to — after routers — use
[`Model.get_connection()`](../models/model-methods.md#get-connection) or `QuerySet.get_connection()`.

### <a id="reconnect"></a>Reconnecting after the database was replaced

```python
await Connections.reconnect()
```

Closes every connection and pool of the current context and keeps their configuration; the next query
opens new connections. Use it when the database was replaced under the running application: a SQLite file
swapped for a copy, a PostgreSQL database restored from a dump, or its connections cut with
`pg_terminate_backend()`. Without it the drivers differ on connections the server ended: `rust_pg` checks
a pooled connection before handing it out and opens a new one, while asyncpg finds out only by using it —
the first statement on each such connection raises `DBConnectionError` (a read outside a transaction is
run again with `read_retry_max_retries`), and the pool replaces the connection.

- The connection objects and their names stay the same — code holding `Connections.get("default")` keeps
  working.
- Call it with no transaction open. A query that already holds a connection finishes on it; one still
  waiting for a connection when the old pool closes runs on the new pool — queries running meanwhile in
  other tasks don't fail.
- Each process reconnects its own connections; a worker pool calls it in every worker.
- Without an active context it raises `ConfigurationError`.

### <a id="health-checks"></a>Health checks and unresponsive servers

```python
ok: bool = await Connections.get("default").ping(timeout=5.0)
```

A whole health check — every connection pinged at once, its pools judged by criteria, the readiness route
of a web application — is `HealthCheck` ([Pool metrics and health](../observability/pool-health.md)).

`ping()` runs `SELECT 1` and returns `True`/`False` instead of raising. It always bounds itself with its
own timeout (5 seconds by default, the `timeout=` argument), independent of any connection setting: a
server that stopped answering without closing the socket (network partition, frozen process) makes no
driver raise, so an unbounded call would hang forever. It returns `False` on `DBConnectionError`,
`OperationalError` and on timeout; anything else still propagates. Works the same on every database.

Ordinary queries have no such bound by default — a long migration or bulk operation must not be cut off.
On PostgreSQL a bound is opted into with `command_timeout` (seconds) in the credentials or the URL
(`?command_timeout=30`): a command that outlives it is cancelled and raises `TimeoutError`, and the
connection stays usable — see [`command_timeout`](../dialects/postgresql/connecting.md#command-timeout).
