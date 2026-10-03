from enum import StrEnum


class DistributedTransactionResolution(StrEnum):
    """What a stale prepared transaction found by
    ``DistributedCoordinator.detect_stale_prepared_transactions()`` needs - ``hare distributed-recover``
    issues exactly this against it."""

    COMMIT = "commit"
    ROLLBACK = "rollback"


class TransactionEventType(StrEnum):
    """A real, top-level transaction lifecycle event - never fired for a savepoint (nested
    transaction) begin/release/rollback, only for the outermost transaction on a connection."""

    BEGIN = "begin"
    COMMIT = "commit"
    ROLLBACK = "rollback"


class IsolationLevel(StrEnum):
    """A transaction isolation level of the SQL standard, weakest first - each one rules out the
    anomalies of every level before it."""

    READ_UNCOMMITTED = "read uncommitted"
    READ_COMMITTED = "read committed"
    REPEATABLE_READ = "repeatable read"
    SERIALIZABLE = "serializable"
