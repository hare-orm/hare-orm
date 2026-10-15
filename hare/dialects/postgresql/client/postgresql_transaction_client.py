from __future__ import annotations

import abc
import uuid
from typing import NoReturn

from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.postgresql.client.constants import (
    POSTGRES_ABORTED_TRANSACTION_COMMIT_MESSAGE,
    POSTGRES_SAVEPOINT_NAME_TEMPLATE,
)
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.exceptions import (
    TransactionManagementError,
)
from hare.transactions.enums import TransactionEventType


class PostgresqlTransactionClient(TransactionClient, abc.ABC):
    """What the transaction clients of both PostgreSQL drivers share."""

    begin = PostgresqlClient.translate_exceptions(TransactionClient.begin)
    savepoint = PostgresqlClient.translate_exceptions(TransactionClient.savepoint)
    release_savepoint = PostgresqlClient.translate_exceptions(TransactionClient.release_savepoint)
    savepoint_rollback = PostgresqlClient.translate_exceptions(TransactionClient.savepoint_rollback)
    commit = PostgresqlClient.translate_exceptions(TransactionClient.commit)
    rollback = PostgresqlClient.translate_exceptions(TransactionClient.rollback)

    @abc.abstractmethod
    async def _driver_check_alive(self) -> None:
        """Runs a statement on the transaction - it fails once PostgreSQL has aborted it."""

    @abc.abstractmethod
    def _is_aborted_error(self, error: BaseException) -> bool:
        """Whether a driver error says PostgreSQL has aborted the transaction.

        Args:
            error: The error the driver raised.
        """

    def _get_new_savepoint_name(self) -> str:
        return POSTGRES_SAVEPOINT_NAME_TEMPLATE.format(unique_id=uuid.uuid4().hex)

    async def _before_top_level_end(self, event: TransactionEventType) -> Exception | None:
        if event is TransactionEventType.COMMIT and self._statement_failed:
            await self._roll_back_if_aborted()
        return None

    async def _roll_back_if_aborted(self) -> None:
        """Before the COMMIT of a transaction in which a statement failed: rolls it back and
        raises instead when a statement was interrupted or PostgreSQL has aborted the transaction,
        which would otherwise answer the COMMIT with a silent ROLLBACK.

        Raises:
            TransactionManagementError: The transaction was aborted and has been rolled back.
        """
        if self._statement_interrupted:
            await self._roll_back_after_abort(None)
        try:
            await self._driver_check_alive()
        except BaseException as error:
            if not self._is_aborted_error(error):
                raise
            await self._roll_back_after_abort(error)

    async def _roll_back_after_abort(self, cause: BaseException | None) -> NoReturn:
        """Rolls back a transaction that can't commit and raises the aborted-commit error.

        Args:
            cause: The driver error that revealed the abort, if any.

        Raises:
            TransactionManagementError: Always.
        """
        self._pending_top_level_operation = None
        await self._driver_rollback()
        commit_error = TransactionManagementError(POSTGRES_ABORTED_TRANSACTION_COMMIT_MESSAGE)
        await TransactionEnding.finish_after_failed_commit(self, commit_error)
        raise commit_error from cause
