from __future__ import annotations

#: Sent before the COMMIT of a transaction in which a statement failed - Postgres refuses it with
#: SQLSTATE 25P02 when the transaction is aborted, where it would answer the COMMIT itself with a
#: silent ROLLBACK.
POSTGRES_TRANSACTION_ALIVE_CHECK_SQL = "SELECT 1"

POSTGRES_SAVEPOINT_SQL = "SAVEPOINT {name}"

POSTGRES_RELEASE_SAVEPOINT_SQL = "RELEASE SAVEPOINT {name}"

POSTGRES_ROLLBACK_TO_SAVEPOINT_SQL = "ROLLBACK TO SAVEPOINT {name}"

#: ``sslmode`` values the rust_pg driver's ``ssl_mode`` connect argument accepts.
RUST_PG_SSL_MODES = frozenset({"disable", "prefer", "require", "verify-ca", "verify-full"})

#: The server's refusal of a prepared statement whose result a schema change altered, and its SQLSTATE.
POSTGRESQL_STALE_PLAN_MESSAGE = "cached plan must not change result type"

POSTGRESQL_STALE_PLAN_SQLSTATE = "0A000"

POSTGRES_SHELL_SSL_MODE_VARIABLE = "PGSSLMODE"

POSTGRES_SHELL_SSL_ROOT_CERT_VARIABLE = "PGSSLROOTCERT"

#: The libpq ``sslmode`` of ``ssl=true``/``ssl=false``.
POSTGRES_SHELL_SSL_MODE_BY_SWITCH = {True: "require", False: "disable"}
