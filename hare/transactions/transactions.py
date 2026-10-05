from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from hare.core.log import logger
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.exceptions import (
    QueryError,
)
from hare.transactions.atomic.atomic import Atomic
from hare.transactions.constants import MAX_TRANSACTION_RETRIES
from hare.transactions.distributed.distributed_coordinator import DistributedCoordinator
from hare.transactions.distributed.distributed_transactions import DistributedTransactions
from hare.transactions.enums import IsolationLevel
from hare.transactions.transaction_options import TransactionOptions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient

P = ParamSpec("P")
T = TypeVar("T")


class Transactions:
    """Transaction management: resolving the target connection for a name/default, and
    running or wrapping code inside a transaction on it."""

    @staticmethod
    def atomic(
        using: str | None = None,
        *,
        read_only: bool = False,
        statement_timeout: float | None = None,
        isolation: IsolationLevel | str | None = None,
        lock_timeout: float | None = None,
        retries: int = 0,
    ) -> Atomic:
        """A transaction - ``async with Transactions.atomic():`` runs the block in it, and
        ``@Transactions.atomic()`` runs every call of the decorated function in one of its own.
        An error leaving the block rolls the transaction back.

        Args:
            retries: How many more times the transaction runs after a ``TransactionRetryError``
                (a serialization failure, a deadlock) of its block or its commit - an int from 0 to
                ``MAX_TRANSACTION_RETRIES``. The decorated function is called again; a block runs as
                ``async for attempt in Transactions.atomic(retries=3): async with attempt: ...``. A
                transaction nested in another never runs again - the outer one does.
            using: The connection's name, optional with a single connection.
            read_only: Make the database itself refuse every write inside the block
                (``SET TRANSACTION READ ONLY`` on PostgreSQL, ``PRAGMA query_only`` on SQLite).
            statement_timeout: Seconds any single statement inside the block may run before the
                database cancels it (PostgreSQL also applies it as ``lock_timeout``).
            isolation: The isolation level the transaction runs at - an ``IsolationLevel`` or its
                name (``"serializable"``); None for the database's default. The database runs it
                at the weakest level it has that is at least as strong - every SQLite transaction
                is serializable. A nested block runs at the level of the transaction it is nested
                in, and may only repeat that level.
            lock_timeout: Seconds a statement inside the block may wait for a lock another session
                holds before it fails (``lock_timeout`` on PostgreSQL, the busy timeout on SQLite).

        Returns:
            The transaction, started when its block is entered or the decorated function is called.

        Raises:
            QueryError: ``read_only``/``statement_timeout``/``isolation``/``retries`` has the wrong type or
                range; entering the block - a read-only transaction nested inside a read-write
                one, or a nested transaction setting its own ``statement_timeout`` or another
                isolation level.
            UnSupportedError: Entering the block - the database has no transactions
                (``Features.supports_transactions``), or no isolation level at least as strong as
                ``isolation``.
        """
        options = TransactionOptions(
            read_only=read_only,
            statement_timeout=statement_timeout,
            isolation=isolation,  # type: ignore[arg-type]
            lock_timeout=lock_timeout,
        )
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_TRANSACTION_RETRIES:
            raise QueryError(f"atomic(retries=...) takes an int from 0 to {MAX_TRANSACTION_RETRIES}, got {retries!r}")
        return Atomic(partial(Atomic.get_connection, using), options, retries)

    @staticmethod
    def on_commit(callback: Callable[[], Any] | Callable[[], Awaitable[Any]], using: str | None = None) -> None:
        """Runs ``callback`` once the current transaction on ``using`` commits - not when a savepoint
        releases; with no open transaction, at once. Callbacks run after the transaction, at the
        level it was opened from, so they can query and open transactions. A callback registered in
        a savepoint that rolls back is discarded. A transaction already ended counts as none.

        Args:
            callback: A callable or coroutine function taking no arguments.
            using: The connection name - optional with one connection.
        """
        connection = Atomic.get_connection(using)
        if isinstance(connection, TransactionClient) and not connection._is_transaction_finished():
            TransactionCallbacks.add_on_commit_callback(connection, callback)
            return
        if inspect.iscoroutinefunction(callback):
            # Checked before calling it - calling a coroutine function only to inspect the result
            # leaves an unawaited coroutine.
            raise QueryError(
                "on_commit() outside a transaction can't run an async callback synchronously - "
                "await it yourself, or call on_commit() from inside a transaction."
            )
        result = callback()
        if inspect.isawaitable(result):
            raise QueryError(
                "on_commit() outside a transaction can't run an async callback synchronously - "
                "await it yourself, or call on_commit() from inside a transaction."
            )

    @staticmethod
    def on_rollback(callback: Callable[[], Any] | Callable[[], Awaitable[Any]], using: str | None = None) -> None:
        """Runs ``callback`` if the current transaction on ``using`` - or, registered in a savepoint,
        that savepoint - rolls back. A callback of a released savepoint stays live for the outer
        transaction. For a compensating action of a write made through ``autonomous()``. A
        transaction already ended can't roll back: the callback never runs, and a warning is logged.

        Args:
            callback: A callable or coroutine function taking no arguments.
            using: The connection name - optional with one connection.

        Raises:
            QueryError: There is no open transaction on ``using``.
        """
        connection = Atomic.get_connection(using)
        if not isinstance(connection, TransactionClient):
            raise QueryError(
                "on_rollback() outside a transaction has nothing to attach to - wrap it in "
                "Transactions.atomic() first."
            )
        if connection._is_transaction_finished():
            logger.warning(
                "on_rollback() called after the transaction on %r ended - %r will never run",
                connection.connection_alias,
                callback,
            )
            return
        TransactionCallbacks.add_on_rollback_callback(connection, callback)

    @staticmethod
    @asynccontextmanager
    async def autonomous(
        using: str | None = None,
        *,
        read_only: bool = False,
        statement_timeout: float | None = None,
        isolation: IsolationLevel | str | None = None,
        lock_timeout: float | None = None,
    ) -> AsyncGenerator[DatabaseClient]:
        """Yields a new connection to the database of ``using``, apart from any transaction open on the
        shared one - a write on it (``using=...``) commits on its own. Closed when the block exits.
        On SQLite ``:memory:`` gives each connection its own database, and writers block each other
        on one file. With ``read_only``, ``statement_timeout``, ``isolation`` or ``lock_timeout`` the block runs in
        one transaction of its own with those options, as ``atomic()`` takes them.

        Args:
            using: The connection whose configuration is used - optional with one connection.
            read_only: As ``atomic(read_only=...)``.
            statement_timeout: As ``atomic(statement_timeout=...)``.
            isolation: As ``atomic(isolation=...)``.
            lock_timeout: As ``atomic(lock_timeout=...)``.

        Raises:
            QueryError: An option has the wrong type or range.
            UnSupportedError: The dialect doesn't support an option.
        """
        options = TransactionOptions(
            read_only=read_only,
            statement_timeout=statement_timeout,
            isolation=isolation,  # type: ignore[arg-type]
            lock_timeout=lock_timeout,
        )
        base_connection = Atomic.get_connection(using)
        connection = base_connection.create_independent_client()
        try:
            if options == TransactionOptions.DEFAULT:
                yield connection
            else:
                async with connection._in_transaction(options) as transaction_client:
                    yield transaction_client
        finally:
            await connection.close()

    @staticmethod
    def distributed(
        coordinator: str, participants: Sequence[str]
    ) -> AbstractAsyncContextManager[DistributedTransactions]:
        """Two-phase commit across several databases: every participant is prepared (``PREPARE
        TRANSACTION``) before anything commits, and the coordinator's commit of a row into
        ``hare_distributed_decisions`` is the instant the transaction happens - participants then
        only need ``COMMIT PREPARED``, which ``hare distributed-recover`` can finish later. Every
        connection needs ``Features.supports_two_phase_commit``.

        Example::

            async with Transactions.distributed(coordinator="db1", participants=["db2", "db3"]) as
            txns:
                await Widget.objects.using(txns.coordinator).create(...)
                await Job.objects.using(txns["db2"]).create(...)

        Args:
            coordinator: The alias whose commit is the decision.
            participants: The other aliases - non-empty, without ``coordinator``.

        Raises:
            QueryError: ``participants`` is empty or repeats an alias, or an alias is already inside
                a transaction.
            UnSupportedError: An alias has no two-phase commit.
            ConfigurationError: PREPARE fails for want of a prepared transaction slot
                (``max_prepared_transactions = 0``, or every slot taken).
            DistributedTransactionCommitAmbiguousError: The coordinator's commit failed with an
                unknown outcome - run ``hare distributed-recover``.
            DistributedTransactionPartiallyCommittedError: The transaction committed but some
                participants didn't finish ``COMMIT PREPARED`` - run ``hare distributed-recover``.
                Their ``on_commit()`` callbacks don't run.
            BaseExceptionGroup: Everything committed but several ``on_commit()`` callbacks raised -
                one failure is raised as is.
        """
        return DistributedCoordinator.run(coordinator, participants)
