from __future__ import annotations

from dataclasses import dataclass

from hare.transactions.enums import TransactionOutcome


@dataclass(frozen=True, slots=True)
class TransactionGroupExit:
    """One connection's requested exit and its error, in cleanup order.

    An exit error does not prove that COMMIT failed: a server outcome may be unknown, or a
    callback may have failed after a successful commit. Retrying requires a confirmed rollback
    of every participant, independently of the exception class.
    """

    connection_alias: str | None
    commit_requested: bool
    error: BaseException | None
    outcome: TransactionOutcome = TransactionOutcome.UNKNOWN
