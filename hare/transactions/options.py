from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from hare.exceptions import QueryError
from hare.transactions.constants import (
    MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS,
    MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS,
)
from hare.transactions.enums import IsolationLevel


@dataclass(frozen=True, slots=True)
class TransactionOptions:
    """How a transaction runs - given to ``Transactions.atomic()`` and kept by
    the transaction's client for as long as it is open. A nested transaction (a savepoint) runs
    with the options of the transaction it is nested in.

    Attributes:
        read_only: Whether the database itself refuses every write in the transaction.
        statement_timeout: Seconds any single statement in the transaction may run before the
            database cancels it, None for no limit.
        isolation: The isolation level the transaction runs at, None for the database's default.
            A dialect runs a transaction at the weakest level it has that is at least as strong
            as the one asked for - the SQL standard lets a database run a transaction at a
            stronger level than requested - and refuses a level stronger than any it has.
    """

    #: The options of a transaction opened without any: read-write, no statement timeout, the
    #: database's default isolation level.
    DEFAULT: ClassVar[TransactionOptions]

    read_only: bool = False
    statement_timeout: float | None = None
    isolation: IsolationLevel | None = None

    def __post_init__(self) -> None:
        """Checks the type and range of every option, and turns an isolation level given by its
        name (``"serializable"``) into an ``IsolationLevel``.

        Raises:
            QueryError: ``read_only`` isn't a bool, ``statement_timeout`` isn't None or a number
                of seconds within the supported range, or ``isolation`` names no isolation level.
        """
        if not isinstance(self.read_only, bool):
            raise QueryError(f"read_only must be a bool, got {self.read_only!r}")
        self._check_statement_timeout(self.statement_timeout)
        given_isolation: Any = self.isolation
        if given_isolation is not None and not isinstance(given_isolation, IsolationLevel):
            try:
                isolation = IsolationLevel(given_isolation)
            except ValueError:
                raise QueryError(
                    f"isolation must be one of {[str(level) for level in IsolationLevel]}, got {given_isolation!r}"
                ) from None
            object.__setattr__(self, "isolation", isolation)

    @staticmethod
    def _check_statement_timeout(statement_timeout: Any) -> None:
        """Checks that ``statement_timeout`` is None or a number of seconds within the supported range.

        Args:
            statement_timeout: The value given.

        Raises:
            QueryError: It isn't.
        """
        if statement_timeout is None:
            return
        if isinstance(statement_timeout, bool) or not isinstance(statement_timeout, int | float):
            raise QueryError(f"statement_timeout must be a number of seconds, got {statement_timeout!r}")
        if not (
            MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS <= statement_timeout <= MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS
        ):
            raise QueryError(
                f"statement_timeout must be between {MIN_TRANSACTION_STATEMENT_TIMEOUT_SECONDS} and "
                f"{MAX_TRANSACTION_STATEMENT_TIMEOUT_SECONDS} seconds, got {statement_timeout!r}"
            )


TransactionOptions.DEFAULT = TransactionOptions()
