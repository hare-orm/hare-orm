# Pool metrics and health

What a pool of connections holds and has done, and how usable each connection is: a snapshot of
every pool, the events of a wait that ran out and of a failed connect, the pool metrics in
OpenTelemetry, and a health check with criteria of your choice — for a readiness probe, a monitoring
page or an alert.

## <a id="pool-status"></a>Pool status

```python
from hare import Connections

status = Connections.get("default").get_pool_status()
status.size, status.idle, status.in_use, status.waiting   # 20, 0, 20, 3

for status in Connections.get_pool_statuses():            # every open pool of the current context
    print(status.connection_alias, status.role, status.schema, status.in_use, status.waiting)
```

`get_pool_status()` returns a `PoolStatus` (`hare.instrumentation.declarations`), or None while the
client's pool isn't open yet:

| Field | What it is |
|---|---|
| `connection_alias` | The connection of the configuration. |
| `role` | Which client of the connection the pool belongs to (below). |
| `schema` | The tenant schema of a tenant schema's pool, None otherwise. |
| `size`, `idle`, `in_use` | The open connections, the idle ones and the taken ones. |
| `waiting` | The tasks waiting for a connection right now. |
| `min_size`, `max_size` | The pool's bounds. |
| `acquire_count` | How many times a connection was taken. |
| `acquire_timeouts` | How many waits for a connection ran out of `pool_acquire_timeout`. |
| `acquire_wait_seconds_total` | How long the waits took in all — measured only while `PoolMetrics` is enabled. |
| `connect_count`, `connect_failures` | The connections the pool opened, and the failures to open one. |

The counters belong to the client, not to its pool: they survive `Connections.reconnect()`, which
replaces the pools. On SQLite the "pool" is the client's one connection — `size` is 1 and `waiting`
counts the tasks and transactions waiting for it.

A connection may have several pools, each with its `PoolRole` (`hare.instrumentation.enums`):

| Role | The pool of |
|---|---|
| `OWN` | The connection's own client. |
| `TENANT_SCHEMA` | The client of one tenant's schema ([schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md)). |
| `DIRECT` | The client of the server itself past a transaction pooler (`direct_host`, [Connection poolers](../connections/connection-poolers.md)). |
| `INDEPENDENT` | A client of its own — `Transactions.autonomous()`, a migration's session with a lock timeout. |

`Connections.get_pool_statuses(connection_alias=None)` lists every open pool of the current context,
by connection, role and schema. A client whose driver reports no pool status
(`Features.supports_pool_status` false — a third-party dialect) is not in the list, and its
`get_pool_status()` raises `UnSupportedError`.

## <a id="pool-metrics"></a>Measuring the waits: `PoolMetrics`

How long each wait for a connection takes is measured only while the pool metrics are enabled —
without them, taking a connection costs no call at all:

```python
from hare.instrumentation.pools.pool_metrics import PoolMetrics

PoolMetrics.enable()
...
PoolMetrics.disable()
```

Each `enable()` is matched by a `disable()` — the measuring stops once every user disabled it. The
OpenTelemetry instrumentor enables it while instrumented. The time a connection takes to open is
always measured: it happens once per connection, never on the query path.

## <a id="events"></a>Events

Two [observer](observers.md) events, sent only on the failure:

- `PoolAcquireTimedOut` — a wait for a connection ran out of `pool_acquire_timeout`; the call
  raises `PoolTimeoutError` (a `DBConnectionError`). Carries the connection, role, schema, how long
  the call waited, and the pool's `size`, `max_size` and `waiting` at that moment.
- `ConnectionFailed` — opening a connection failed: opening a pool (each attempt of
  `connect_max_retries`), or a pool growing. Carries the connection, role, the server's address,
  the exception's type name (never its text — it may hold the user and host), the attempt and
  whether another attempt follows.

```python
from hare.instrumentation import Observers
from hare.instrumentation.declarations import PoolAcquireTimedOut

Observers.observe(PoolAcquireTimedOut, lambda event: alert(f"{event.connection_alias} pool exhausted"))
```

## <a id="opentelemetry-metrics"></a>OpenTelemetry metrics

`OpenTelemetryInstrumentor` ([OpenTelemetry](opentelemetry.md)) reports every open pool of the
process, named by OpenTelemetry's semantic conventions for database clients (their status is still
"development"):

| Metric | Type | Value |
|---|---|---|
| `db.client.connection.count` | up-down counter, `db.client.connection.state` = `idle`/`used` | `idle`, `in_use` |
| `db.client.connection.max` | up-down counter | `max_size` |
| `db.client.connection.idle.min` | up-down counter | `min_size` |
| `db.client.connection.pending_requests` | up-down counter | `waiting` |
| `db.client.connection.timeouts` | counter | `acquire_timeouts` |
| `db.client.connection.wait_time` | histogram, seconds | each wait for a connection |
| `db.client.connection.create_time` | histogram, seconds | each connect |
| `hare.pool.connect_failures` | counter | `connect_failures` |

Every metric carries `db.client.connection.pool.name` (the connection), `hare.pool.role`,
`db.system.name` and, for a tenant schema's pool, `hare.pool.schema`. Pools with equal attributes —
one connection of several `HareContext` objects in a process — are summed.

The values are read when the metrics are collected, on the exporter's thread. A histogram can't be
observed, so the waits and connects measured since the previous collection are recorded during the
collection. Each pool keeps its latest 4096 durations of each type
for its readers; a reader further behind loses the oldest ones.

## <a id="health-check"></a>Health check

```python
from hare.health import ConnectionCriteria, HealthCheck
from hare.health.criteria import PingFailed, PoolSaturation, WaitingRequests

health_check = HealthCheck(
    timeout_seconds=2.0,
    degraded_when=[WaitingRequests(at_least=1), PoolSaturation(ratio=0.9)],   # the default
    unhealthy_when=[PingFailed()],                                            # the default
    criteria_by_connection={"analytics": ConnectionCriteria(degraded_when=[PoolSaturation(ratio=1.0)])},
)
report = await health_check.run()
report.status                                    # HealthStatus.DEGRADED
report.connections["default"].reasons            # ("3 tasks wait for a connection of the own pool (at least 1)",)
```

`run()` pings every connection of the current context at once, each within `timeout_seconds`, and
judges it by the criteria: an unhealthy criterion holding makes it `UNHEALTHY`, else a degrading one
`DEGRADED`, else it is `HEALTHY`. The report's `status` is its worst connection's.

| Argument | Default | What it is |
|---|---|---|
| `timeout_seconds` | 2.0 | How long a ping waits — above 0, at most 60. |
| `degraded_when` | `[WaitingRequests(at_least=1), PoolSaturation(ratio=0.9)]` | The criteria making a connection degraded — `[]` for never. |
| `unhealthy_when` | `[PingFailed()]` | The criteria making a connection unhealthy. |
| `criteria_by_connection` | none | Other criteria for some connections (`ConnectionCriteria`); a list it leaves None is the check's own. |
| `connections` | every connection | The connections checked. |
| `check_direct` | False | Ping the server past a connection's transaction pooler (`direct_host`) too. |

- `run()` raises nothing about the database: a ping failing — a refused connection, a wrong
  password, a server that doesn't answer in time — is an unhealthy status with the exception's type
  name. It raises `ConfigurationError` only with no current context, or for a connection name the
  configuration lacks.
- `run(ping=False)` judges only the pools' state and opens no pool — `PingFailed` and
  `PingLatency` never hold then.
- Keep one `HealthCheck` object: the criteria counting "since the last check" compare with its
  previous run.
- Every argument is checked by type and range when the object is made.

### <a id="criteria"></a>Criteria

| Criterion (`hare.health.criteria`) | Holds when |
|---|---|
| `PingFailed()` | the ping failed or didn't answer in time — or, with `check_direct`, the ping of the server past the pooler |
| `PingLatency(max_ms)` | the ping took longer than `max_ms` (above 0, at most 60000) |
| `PoolSaturation(ratio)` | a pool has at least `ratio` of its `max_size` in use (above 0, at most 1) |
| `WaitingRequests(at_least)` | at least `at_least` tasks wait for a connection of a pool |
| `AcquireWaitTime(max_seconds)` | a wait for a connection since the last check took longer than `max_seconds` — needs `PoolMetrics` enabled when the check is made, else `ConfigurationError` |
| `AcquireTimeouts(at_least)` | at least `at_least` waits ran out since the last check |
| `ConnectFailures(at_least)` | opening a connection failed at least `at_least` times since the last check |
| `AllOf(*criteria)` | every one of its criteria holds |

A criterion of your own subclasses `HealthCriterion` and returns its reason from `check()`:

```python
from hare.health.criteria import HealthCriterion

class TenantPoolsBusy(HealthCriterion):
    def check(self, connection_check):
        busy = [pool for pool in connection_check.pools if pool.status.schema and pool.status.waiting]
        return f"{len(busy)} tenant pools have waiting tasks" if len(busy) > 2 else None
```

`connection_check` (`ConnectionCheck`) holds the ping, the ping of the server past the pooler, and
each pool (`PoolChange`): its status, and what it did since the last check — connections taken,
timeouts, failed connects, the measured waits.

### <a id="report"></a>The report

`HealthReport.to_dict()` gives the report as JSON values — only the statuses by default, for an
answer anyone may read; `to_dict(include_details=True)` adds each connection's ping latency, the
reasons, the exception's type name and its pools:

```json
{
  "status": "degraded",
  "checked_at": "2026-10-06T14:03:11.204871+00:00",
  "connections": {
    "default": {
      "status": "degraded",
      "latency_ms": 1.8,
      "reasons": ["3 tasks wait for a connection of the own pool (at least 1)"],
      "error_type": null,
      "pools": [{"connection_alias": "default", "role": "own", "schema": null, "size": 20, "idle": 0,
                 "in_use": 20, "waiting": 3, "min_size": 5, "max_size": 20, "acquire_count": 18211,
                 "acquire_timeouts": 0, "acquire_wait_seconds_total": 3.42, "connect_count": 20,
                 "connect_failures": 0}]
    }
  }
}
```

The text of an exception never gets into the report — it may hold the server's address and user.

### <a id="liveness-and-readiness"></a>Liveness and readiness

A readiness probe asks "can this instance take traffic" — `HealthCheck.run()`. A liveness probe asks
"is the process alive" and must not touch the database: if it failed while the database is down,
the orchestrator would restart every instance of the application during the outage. The framework
integrations add both routes ([FastAPI](../integrations/fastapi.md#health-routes),
[Litestar](../integrations/litestar.md#health-routes), [Robyn](../integrations/robyn.md#health-routes)):

```python
from hare.contrib.frameworks.health_routes import HealthRoutes

app = HareFastAPI(
    hare_config=HARE_CONFIG,
    health_routes=HealthRoutes(
        health_check,
        readiness_path="/health/ready",   # the default
        liveness_path="/health/live",     # the default
        degraded_status_code=200,         # the default: a degraded instance still takes traffic
        include_details=False,            # the default: only the statuses
    ),
)
```

| Status | `/health/ready` | `/health/live` |
|---|---|---|
| healthy | 200 | 200 |
| degraded | `degraded_status_code` | 200 |
| unhealthy | 503 | 200 |

Neither route runs in the request's transaction (`atomic_requests`) or appears in the OpenAPI
schema.

```yaml
readinessProbe: {httpGet: {path: /health/ready, port: 8000}, periodSeconds: 5, timeoutSeconds: 3}
livenessProbe:  {httpGet: {path: /health/live,  port: 8000}, periodSeconds: 10}
```

### <a id="behind-pgbouncer"></a>Behind PgBouncer

Through a transaction pooler the ping reaches the server through the pooler. `check_direct=True`
pings the server itself as well, by the connection's `direct_host`/`direct_port`
([Connection poolers](../connections/connection-poolers.md)): a pooler that is up while the server
is down — or the other way round — shows. A connection through a pooler without `direct_host` fails
that ping with `ConfigurationError`.
