from __future__ import annotations

#: The transaction-local timeouts of `Transactions.atomic(statement_timeout=...)`, in whole
#: milliseconds.
POSTGRES_SET_LOCAL_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = {milliseconds}"
POSTGRES_SET_LOCAL_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = {milliseconds}"
#: The lock timeout of every later statement of the connection, in whole milliseconds.
POSTGRES_SET_SESSION_LOCK_TIMEOUT_SQL = "SET lock_timeout = {milliseconds}"
#: The setting holding the tenants of a transaction on a connection with
#: ``tenant_row_level_security`` - a JSON array of the tenant values as text, or
#: ``POSTGRESQL_ALL_TENANTS_SETTING_VALUE``; unset (NULL or '') - no tenant.
POSTGRESQL_TENANT_SETTING_NAME = "hare.tenant"
POSTGRESQL_ALL_TENANTS_SETTING_VALUE = "*"
#: Sets the tenants of the transaction - ``value`` is a string literal.
POSTGRESQL_SET_TENANT_SETTING_TEMPLATE = "SELECT set_config('{setting}', {value}, true)"

#: Lower-cased messages PREPARE TRANSACTION fails with when the server has
#: max_prepared_transactions = 0, and when every one of its slots is already taken.
POSTGRESQL_PREPARED_TRANSACTIONS_DISABLED_MESSAGE = "prepared transactions are disabled"

POSTGRESQL_PREPARED_TRANSACTIONS_LIMIT_REACHED_MESSAGE = "maximum number of prepared transactions reached"
