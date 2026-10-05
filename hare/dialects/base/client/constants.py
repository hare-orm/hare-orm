from __future__ import annotations

#: How long a shielded COMMIT/ROLLBACK/savepoint statement is waited for after its task was
#: cancelled - a dead server must not hang the cancellation.
SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS = 30.0

#: How long a top-level COMMIT/ROLLBACK waits for the statements and savepoints other tasks still
#: run on the same transaction (asyncio.gather()/TaskGroup siblings) before giving up - a COMMIT
#: then fails with TransactionManagementError and nothing is committed, a ROLLBACK goes ahead
#: anyway.
TRANSACTION_END_WAIT_TIMEOUT_SECONDS = 30.0

#: The bound of DatabaseClient.ping() - a server that stopped answering without closing the socket
#: never makes the driver raise.
PING_TIMEOUT_SECONDS = 5.0

#: The statement of DatabaseClient.ping() - standard SQL every database runs.
PING_SQL = "SELECT 1"

#: The text of a PoolTimeoutError - waiting for a connection of a pool ran out of
#: ``pool_acquire_timeout``.
POOL_TIMEOUT_MESSAGE = "Timed out after {seconds}s waiting for a free connection of the pool of {connection_alias!r}"

#: The error of a statement or an ending on a transaction that ended already.
TRANSACTION_FINALISED_MESSAGE = "Transaction already finalised"
