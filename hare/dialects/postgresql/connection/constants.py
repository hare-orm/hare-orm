from __future__ import annotations

#: libpq ``sslmode`` values, all accepted by asyncpg's ``ssl`` connect argument.
POSTGRES_SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})

#: DB_URL query parameters that all select the TLS mode of a Postgres connection.
POSTGRES_SSL_QUERY_PARAMETERS = ("ssl", "sslmode", "ssl_mode")

#: SQLSTATEs of a statement the server aborted because of a concurrent transaction - running the
#: transaction again can succeed: 40001 serialization_failure, 40P01 deadlock_detected.
POSTGRESQL_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
