from __future__ import annotations

#: OpenTelemetry semantic-convention attribute naming the database system (``sqlite``, ``postgresql``).
DB_SYSTEM_NAME_ATTRIBUTE = "db.system.name"
#: OpenTelemetry semantic-convention attribute holding the SQL text of a query.
DB_QUERY_TEXT_ATTRIBUTE = "db.query.text"

#: The pool metrics, as OpenTelemetry's semantic conventions for database clients name them (and one
#: of hare's own, the failures to connect).
CONNECTION_COUNT_METRIC = "db.client.connection.count"
CONNECTION_MAX_METRIC = "db.client.connection.max"
CONNECTION_IDLE_MIN_METRIC = "db.client.connection.idle.min"
CONNECTION_PENDING_REQUESTS_METRIC = "db.client.connection.pending_requests"
CONNECTION_TIMEOUTS_METRIC = "db.client.connection.timeouts"
CONNECTION_WAIT_TIME_METRIC = "db.client.connection.wait_time"
CONNECTION_CREATE_TIME_METRIC = "db.client.connection.create_time"
CONNECT_FAILURES_METRIC = "hare.pool.connect_failures"
#: The attributes of the pool metrics.
CONNECTION_POOL_NAME_ATTRIBUTE = "db.client.connection.pool.name"
CONNECTION_STATE_ATTRIBUTE = "db.client.connection.state"
POOL_ROLE_ATTRIBUTE = "hare.pool.role"
POOL_SCHEMA_ATTRIBUTE = "hare.pool.schema"
#: The values of ``db.client.connection.state``.
IDLE_CONNECTION_STATE = "idle"
USED_CONNECTION_STATE = "used"
#: The units of the pool metrics.
CONNECTION_UNIT = "{connection}"
REQUEST_UNIT = "{request}"
TIMEOUT_UNIT = "{timeout}"
FAILURE_UNIT = "{failure}"
SECONDS_UNIT = "s"
