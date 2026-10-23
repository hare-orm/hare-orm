from __future__ import annotations

#: How long a health check waits for a connection's ping by default, in seconds.
DEFAULT_HEALTH_TIMEOUT_SECONDS = 2.0
#: The longest timeout of a health check's ping, in seconds - a sanity ceiling against typos.
MAX_HEALTH_TIMEOUT_SECONDS = 60.0
#: The longest wait for a connection ``AcquireWaitTime`` takes, in seconds.
MAX_ACQUIRE_WAIT_SECONDS = 3600.0
#: The longest ping ``PingLatency`` takes, in milliseconds.
MAX_PING_LATENCY_MS = 60_000.0
#: The default share of a pool's connections in use at which ``PoolSaturation`` holds.
DEFAULT_POOL_SATURATION_RATIO = 0.9

#: The reasons the criteria give - every one names what it saw and its own threshold.
PING_FAILED_REASON = "the ping failed: {error_type}"
SERVER_PING_FAILED_REASON = "the ping of the server past the pooler failed: {error_type}"
PING_LATENCY_REASON = "the ping took {latency_ms:.1f} ms (over {max_ms:g} ms)"
POOL_SATURATION_REASON = "{in_use} of {max_size} connections of {pool} in use (at least {ratio:.0%})"
WAITING_REQUESTS_REASON = "{waiting} tasks wait for a connection of {pool} (at least {at_least})"
ACQUIRE_WAIT_TIME_REASON = "a wait for a connection of {pool} took {seconds:.3f} s (over {max_seconds:g} s)"
ACQUIRE_TIMEOUTS_REASON = "{count} waits for a connection of {pool} ran out since the last check (at least {at_least})"
CONNECT_FAILURES_REASON = "{count} connects of {pool} failed since the last check (at least {at_least})"
#: How a reason names a pool - by its role, and its tenant schema.
POOL_DESCRIPTION = "the {role} pool"
TENANT_SCHEMA_POOL_DESCRIPTION = "the pool of tenant schema {schema}"

#: The text of a criterion requiring the pool metrics while they are off.
POOL_METRICS_REQUIRED_MESSAGE = (
    "{criterion} needs the waits for a connection measured - call PoolMetrics.enable() before making the health check"
)
#: The text of ``criteria_by_connection`` naming a connection the configuration lacks.
UNKNOWN_HEALTH_CONNECTION_MESSAGE = "The health check names connections the configuration lacks: {names}"
