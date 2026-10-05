from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import TransactionRetryError
from hare.transactions.atomic.atomic import Atomic

if TYPE_CHECKING:  # pragma: nocoverage
    from types import TracebackType

    from hare.transactions.atomic.atomic_attempts import AtomicAttempts


class AtomicAttempt(Atomic):
    """One run of ``atomic(retries=N)`` - the transaction of ``async with attempt:``. A
    ``TransactionRetryError`` of its block or of its commit rolls it back and, while runs are left,
    leaves the block quietly so the next run starts. Nested in another transaction it never retries:
    only the outer transaction can run again.

    Args:
        attempts: The runs it is one of.
    """

    __slots__ = ("attempts", "is_nested", "entered")

    def __init__(self, attempts: AtomicAttempts) -> None:
        super().__init__(attempts.get_connection, attempts.options)
        self.attempts = attempts
        self.is_nested = False
        #: Whether the block was entered - an iteration whose run never is would never end.
        self.entered = False

    async def __aenter__(self) -> TransactionClient:
        self.entered = True
        self.is_nested = isinstance(self._get_connection(), TransactionClient)
        return await super().__aenter__()

    async def __aexit__(  # type: ignore[override]
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> bool:
        error = exception
        try:
            await super().__aexit__(exception_type, exception, exception_traceback)
        except TransactionRetryError:
            if exception is not None:
                raise
            # The commit itself failed - the transaction is gone, as after an error of the block.
            if self.is_nested or not self.attempts.take_retry():
                self.attempts.finished = True
                raise
            return True
        if error is None:
            self.attempts.finished = True
            return False
        if isinstance(error, TransactionRetryError) and not self.is_nested and self.attempts.take_retry():
            return True
        self.attempts.finished = True
        return False
