from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.core.connections.connections import Connections
from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan, current_savepoint_span
from hare.dialects.base.transactions.transaction_callback import TransactionCallback

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.transaction_client import TransactionClient


class TransactionCallbacks:
    """The on_commit() and on_rollback() callbacks of a transaction: registered with the savepoint span
    they belong to, run when the top-level transaction ends, and dropped with a savepoint rolled
    back."""

    @staticmethod
    def get_registration_span(client: TransactionClient) -> SavepointSpan | None:
        """The innermost savepoint of this transaction open in the calling context, None at the
        transaction's own level.

        Args:
            client: The transaction's client.
        """
        return client._savepoint_lock.get_own_ancestor_span(current_savepoint_span.get())

    @staticmethod
    def add_on_commit_callback(client: TransactionClient, callback: Callable[[], Any]) -> None:
        """Registers an on_commit() callback at the savepoint level of the calling context.

        Args:
            client: The transaction's client.
            callback: run once the whole transaction commits.
        """
        callback_entry = TransactionCallback(callback, TransactionCallbacks.get_registration_span(client))
        client._get_top_level_transaction()._on_commit_callbacks.append(callback_entry)

    @staticmethod
    def add_on_rollback_callback(client: TransactionClient, callback: Callable[[], Any]) -> None:
        """Registers an on_rollback() callback at the savepoint level of the calling context.

        Args:
            client: The transaction's client.
            callback: run once the transaction, or the savepoint it is registered in, rolls back.
        """
        callback_entry = TransactionCallback(callback, TransactionCallbacks.get_registration_span(client))
        client._get_top_level_transaction()._on_rollback_callbacks.append(callback_entry)

    @staticmethod
    async def run_callbacks(callbacks: list[Callable[[], Any]], label: str) -> None:
        """Runs every callback even if one fails.

        Args:
            callbacks: The callbacks, in registration order.
            label: The callback type for the ExceptionGroup message
                (``"on_commit()"``/``"on_rollback()"``).

        Raises:
            Exception: The one callback failure.
            ExceptionGroup: Every failure, when two or more callbacks failed.
        """
        errors: list[Exception] = []
        for callback in callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup(f"{label} callback failures", errors)

    @staticmethod
    async def run_callbacks_as_outer_level(
        client: TransactionClient, callbacks: list[Callable[[], Any]], label: str
    ) -> None:
        """Runs the callbacks of a finished top-level transaction with this alias pointing back at
        the client the transaction was opened from, so they can query, open new transactions and
        register further on_commit() callbacks like any code outside a transaction.

        Args:
            client: The transaction's client.
            callbacks: the callbacks to run.
            label: names the callback type in an ExceptionGroup message.
        """
        handler = Connections.current()
        token = handler.set(client.connection_alias, client._parent)
        try:
            await TransactionCallbacks.run_callbacks(callbacks, label)
        finally:
            handler.reset(token)

    @staticmethod
    async def discard_rolled_back_savepoint_callbacks(client: TransactionClient) -> None:
        """After this savepoint's ROLLBACK TO landed: drops the ``on_commit()`` callbacks registered
        inside it and runs its ``on_rollback()`` callbacks. Callbacks of other levels are left
        alone.

        Args:
            client: The transaction's client.

        Raises:
            Exception: The one failing callback's exception, or an ExceptionGroup of several.
        """
        savepoint_span = client._savepoint_span
        if savepoint_span is None:
            return
        top_level = client._get_top_level_transaction()
        top_level._on_commit_callbacks = [
            entry for entry in top_level._on_commit_callbacks if not entry.is_registered_within(savepoint_span)
        ]
        fired_entries = [
            entry for entry in top_level._on_rollback_callbacks if entry.is_registered_within(savepoint_span)
        ]
        top_level._on_rollback_callbacks = [
            entry for entry in top_level._on_rollback_callbacks if not entry.is_registered_within(savepoint_span)
        ]
        await TransactionCallbacks.run_callbacks([entry.callback for entry in fired_entries], "on_rollback()")
