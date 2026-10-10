from __future__ import annotations

import abc
import asyncio
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from hare.dialects.base.client.constants import TRANSACTION_FINALISED_MESSAGE
from hare.dialects.base.client.declarations import RowLockOutcome
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_BEGIN_TRANSACTION_SQL,
    CLICKHOUSE_COMMIT_SQL,
    CLICKHOUSE_NO_SAVEPOINTS_MESSAGE,
    CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE,
    CLICKHOUSE_ROLLBACK_SQL,
    CLICKHOUSE_ROW_LOCKS_LOST_MESSAGE,
    CLICKHOUSE_TRANSACTION_ABORTED_MESSAGE,
    CLICKHOUSE_TRANSACTION_CLIENT_ATTRIBUTES,
)
from hare.dialects.clickhouse.keeper.clickhouse_row_locks import ClickhouseRowLocks
from hare.exceptions import TransactionManagementError, UnSupportedError


class ClickhouseTransactionClient(TransactionClient, ClickhouseClient):
    """A ClickHouse transaction (``transactions=true`` connections): a connection of its own - an HTTP
    session, a TCP connection - held for the top-level transaction, its statements sent one at a time.
    The transactions have no savepoints, and the first statement that fails ends the transaction - the
    server takes nothing but its ROLLBACK after it. A driver's transaction client opens and closes the
    connection."""

    #: The client the transaction is opened on.
    _parent: ClickhouseClient
    #: The waits for row locks end at the transaction's lock_timeout.
    enforces_lock_timeout_itself: ClassVar[bool] = True

    def __init__(self, connection: ClickhouseClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        TransactionClient.__init__(self, connection, savepoint_lock)
        for attribute_name in CLICKHOUSE_TRANSACTION_CLIENT_ATTRIBUTES:
            setattr(self, attribute_name, getattr(connection, attribute_name))
        self._connection: Any = None
        self._connection_lock = asyncio.Lock()
        #: Held by the statement running - the server runs one statement of a transaction at a time.
        self._statement_lock = asyncio.Lock()
        #: Whether a statement failed - the server rolled the transaction back.
        self._transaction_aborted = False
        #: The row locks of select_for_update(), made with the first lock.
        self._row_locks: ClickhouseRowLocks | None = None

    @abc.abstractmethod
    async def open_transaction_connection(self) -> Any:
        """Opens the connection the transaction runs on.

        Returns:
            The driver's connection.
        """

    @abc.abstractmethod
    async def close_transaction_connection(self, connection: Any) -> None:
        """Closes the connection the transaction ran on.

        Args:
            connection: The driver's connection.
        """

    @abc.abstractmethod
    async def send_transaction_statement(self, sql: str) -> None:
        """Sends ``BEGIN TRANSACTION``, ``COMMIT`` or ``ROLLBACK`` on the transaction's connection.

        Args:
            sql: The statement.
        """

    async def _take_transaction_resources(self) -> None:
        self._connection = await self.open_transaction_connection()

    async def _give_back_transaction_resources(self) -> None:
        row_locks, self._row_locks = self._row_locks, None
        if row_locks is not None:
            await row_locks.close()
        connection, self._connection = self._connection, None
        if connection is not None:
            await self.close_transaction_connection(connection)

    async def take_statement_connection(self) -> Any:
        """The transaction's connection for one statement, once the statement before has finished.

        Returns:
            The connection.

        Raises:
            TransactionManagementError: The transaction ended, or a statement of it failed.
        """
        self._check_statement_allowed()
        if self._transaction_aborted:
            raise TransactionManagementError(CLICKHOUSE_TRANSACTION_ABORTED_MESSAGE)
        await self._statement_lock.acquire()
        return self._connection

    def give_back_statement_connection(self, exception: BaseException | None) -> None:
        """Ends a statement's hold of the connection - a statement that failed or was interrupted ended
        the transaction.

        Args:
            exception: The error the statement raised, None when it succeeded.
        """
        self._statement_lock.release()
        if exception is not None and not isinstance(exception, TransactionManagementError):
            self._transaction_aborted = True
            self._mark_statement_failed()

    async def take_row_locks(self, lock_names: Sequence[str], *, wait: bool) -> RowLockOutcome:
        transaction = cast("ClickhouseTransactionClient", self._get_top_level_transaction())
        transaction._check_statement_allowed()
        if transaction._transaction_aborted:
            raise TransactionManagementError(CLICKHOUSE_TRANSACTION_ABORTED_MESSAGE)
        row_locks = transaction._row_locks
        if row_locks is None:
            row_locks = transaction._row_locks = self._parent.get_row_locks(
                transaction._transaction_options.lock_timeout
            )
        elif row_locks.is_lost:
            raise TransactionManagementError(CLICKHOUSE_ROW_LOCKS_LOST_MESSAGE)
        return await row_locks.take(lock_names, wait=wait)

    async def _driver_begin(self) -> None:
        await self.send_transaction_statement(CLICKHOUSE_BEGIN_TRANSACTION_SQL)

    async def _driver_commit(self) -> None:
        await self.send_transaction_statement(CLICKHOUSE_COMMIT_SQL)

    async def _driver_rollback(self) -> None:
        try:
            await self.send_transaction_statement(CLICKHOUSE_ROLLBACK_SQL)
        except self.driver_errors.driver:
            # A failed statement may have ended the transaction with its connection - nothing is
            # left to roll back.
            if not self._transaction_aborted:
                raise

    async def _driver_savepoint(self, name: str) -> None:
        raise UnSupportedError(CLICKHOUSE_NO_SAVEPOINTS_MESSAGE)

    async def _driver_release_savepoint(self, name: str) -> None:
        raise UnSupportedError(CLICKHOUSE_NO_SAVEPOINTS_MESSAGE)

    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        raise UnSupportedError(CLICKHOUSE_NO_SAVEPOINTS_MESSAGE)

    def _get_new_savepoint_name(self) -> str:
        raise UnSupportedError(CLICKHOUSE_NO_SAVEPOINTS_MESSAGE)

    def _is_commit_rejection(self, error: BaseException) -> bool:
        return isinstance(error, Exception)

    async def _check_commit_allowed(self) -> None:
        if self._finalized:
            raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)
        transaction = cast("ClickhouseTransactionClient", self._get_top_level_transaction())
        if transaction._transaction_aborted:
            await self.rollback()
            raise TransactionManagementError(f"{CLICKHOUSE_TRANSACTION_ABORTED_MESSAGE} (nothing was committed)")
        if transaction._row_locks is not None and transaction._row_locks.is_lost:
            await self.rollback()
            raise TransactionManagementError(f"{CLICKHOUSE_ROW_LOCKS_LOST_MESSAGE} (nothing was committed)")

    def get_hare_error(self, error: Exception, sql: Any, parameters: Any) -> Exception:
        """hare's exception for a driver's - a statement the server runs in no transaction (of a
        Replicated table, of the schema, of a system table) is refused as such."""
        if self.get_error_code(error) == CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE:
            return UnSupportedError(
                f"ClickHouse runs this statement in no transaction - {error} (sql={sql!r}). A transaction takes "
                "reads and writes of MergeTree tables alone"
            )
        return super().get_hare_error(error, sql, parameters)

    savepoint = ClickhouseClient.translate_exceptions(TransactionClient.savepoint)
    commit = ClickhouseClient.translate_exceptions(TransactionClient.commit)
    rollback = ClickhouseClient.translate_exceptions(TransactionClient.rollback)
