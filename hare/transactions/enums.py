from __future__ import annotations

from enum import StrEnum


class DistributedTransactionResolution(StrEnum):
    """The action proposed by ``DistributedRecovery.run()`` for a prepared transaction.

    The recovery service applies it only with ``finish=True``; the CLI uses the same service.
    """

    COMMIT = "commit"
    ROLLBACK = "rollback"


class TransactionEventType(StrEnum):
    """A top-level transaction lifecycle event, including an uncertain ending.

    Never fired for a savepoint begin/release/rollback. UNKNOWN reports lost confirmation
    of an in-flight commit and must not be interpreted as a rollback.
    """

    BEGIN = "begin"
    COMMIT = "commit"
    ROLLBACK = "rollback"
    UNKNOWN = "unknown"


class TransactionPhase(StrEnum):
    """Local execution phase, independent of a server's durable outcome."""

    NEW = "new"
    ACTIVE = "active"
    ENDING = "ending"
    FINISHED = "finished"


class TransactionOutcome(StrEnum):
    """Known result of a transaction or savepoint; UNKNOWN never implies rollback."""

    PENDING = "pending"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    PREPARED = "prepared"
    RELEASED = "released"
    UNKNOWN = "unknown"


class IsolationLevel(StrEnum):
    """A transaction isolation level of the SQL standard, weakest first - each one rules out the
    anomalies of every level before it."""

    READ_UNCOMMITTED = "read uncommitted"
    READ_COMMITTED = "read committed"
    REPEATABLE_READ = "repeatable read"
    SERIALIZABLE = "serializable"
