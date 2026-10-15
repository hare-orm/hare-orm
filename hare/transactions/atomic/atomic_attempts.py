from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from hare.exceptions import QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.transactions.atomic.atomic_attempt import AtomicAttempt
    from hare.transactions.transaction_options import TransactionOptions


class AtomicAttempts:
    """``async for attempt in Transactions.atomic(retries=N):`` - the runs of a transaction, each
    entered with ``async with attempt:``. A run its block or its commit ends with
    ``TransactionRetryError`` is rolled back and followed by another, up to ``retries`` more; the
    iteration stops after a run that commits, or ends with another error.

    Args:
        get_connection: Gives the connection each run starts on.
        options: How each run's transaction runs.
        retries: How many more runs a ``TransactionRetryError`` may get.
    """

    __slots__ = ("get_connection", "options", "retries_left", "finished", "current_attempt")

    def __init__(
        self, get_connection: Callable[[], DatabaseClient], options: TransactionOptions, retries: int
    ) -> None:
        self.get_connection = get_connection
        self.options = options
        self.retries_left = retries
        #: Whether a run committed or ended with an error nothing retries.
        self.finished = False
        #: The run handed out last, None before the first.
        self.current_attempt: AtomicAttempt | None = None

    def __aiter__(self) -> AtomicAttempts:
        return self

    async def __anext__(self) -> AtomicAttempt:
        """The next run.

        Raises:
            StopAsyncIteration: A run committed, or ended with an error nothing retries.
            QueryError: The run before wasn't entered with ``async with attempt:`` - nothing could
                ever end the iteration.
        """
        # Local import: an attempt is an Atomic, whose module creates these.
        from hare.transactions.atomic.atomic_attempt import AtomicAttempt

        if self.finished:
            raise StopAsyncIteration
        if self.current_attempt is not None and not self.current_attempt.entered:
            self.finished = True
            raise QueryError(
                "A run of atomic(retries=...) wasn't entered - each one runs its block as "
                "`async for attempt in atomic(retries=...): async with attempt: ...`"
            )
        self.current_attempt = AtomicAttempt(self)
        return self.current_attempt

    def take_retry(self) -> bool:
        """Takes one more run, when any is left.

        Returns:
            Whether a run is left.
        """
        if self.retries_left <= 0:
            return False
        self.retries_left -= 1
        return True
