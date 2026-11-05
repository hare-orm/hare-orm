from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TransactionGroupExit:
    """One connection's requested exit and its error, in cleanup order.

    An exit error does not prove that COMMIT failed: a server outcome may be unknown, or a
    callback may have failed after a successful commit. No automatic retry follows it.
    """

    connection_alias: str | None
    commit_requested: bool
    error: BaseException | None
