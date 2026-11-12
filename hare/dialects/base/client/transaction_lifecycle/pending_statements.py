from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.dialects.base.client.constants import TRANSACTION_BEGIN_ABANDONED_MESSAGE
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span
from hare.exceptions import TransactionManagementError

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient


class PendingStatements:
    """The BEGIN and SAVEPOINT statements a transaction defers until its first query, sent then in
    order."""

    @staticmethod
    async def send_pending_transaction_statements(client: TransactionClient) -> None:
        """Sends the transaction's BEGIN and the SAVEPOINTs no statement has sent yet - called with
        the transaction's connection lock held, right before a statement.

        Args:
            client: The transaction's client.
        """
        top_level_transaction = client._outer_transaction or client
        if top_level_transaction._begin_pending:
            await PendingStatements.send_pending_begin(top_level_transaction)
        if top_level_transaction._pending_savepoints:
            await PendingStatements.send_pending_savepoints(client)

    @staticmethod
    async def send_pending_begin(client: TransactionClient) -> None:
        """Sends the transaction's BEGIN when no statement has sent it yet - called with the
        transaction's connection lock held, right before a statement. Shielded: a BEGIN landing
        after the awaiting task was cancelled must be known, or the transaction would never end.

        Args:
            client: The transaction's client.
        """
        top_level_transaction = client._get_top_level_transaction()
        if top_level_transaction._begin_pending:
            if top_level_transaction._shielded_begin_abandoned_task is not None:
                raise TransactionManagementError(TRANSACTION_BEGIN_ABANDONED_MESSAGE)
            await TransactionEnding.run_shielded_from_cancellation(
                top_level_transaction._driver_send_begin(),
                on_landed=top_level_transaction._mark_begin_sent,
                on_timeout=top_level_transaction._mark_begin_operation_abandoned,
            )

    @staticmethod
    async def send_pending_savepoints(client: TransactionClient) -> None:
        """Sends the SAVEPOINT of every savepoint of the transaction no statement has sent yet,
        outermost first - right before a statement that holds its turn on the savepoint lock, so
        every open savepoint encloses it. Each is shielded: a SAVEPOINT landing after the awaiting
        task was cancelled would leave one more savepoint on the connection than the savepoint lock
        tracks.

        Args:
            client: The transaction's client.
        """
        for nested_client in PendingStatements.get_pending_savepoints(client):
            await TransactionEnding.run_shielded_from_cancellation(
                nested_client._driver_savepoint(nested_client._savepoint_name),  # type: ignore[arg-type]
                on_landed=nested_client._drop_pending_savepoint,
            )

    @staticmethod
    def get_pending_savepoints(client: TransactionClient) -> Iterator[TransactionClient]:
        """The unsent savepoints enclosing this call, outermost first.

        Iteration snapshots the pending list because each confirmed SAVEPOINT removes
        its client. Only savepoints enclosing the current span are sent, never a sibling's.
        """
        current_span = current_savepoint_span.get()
        if current_span is not None:
            for nested_client in tuple(client._get_top_level_transaction()._pending_savepoints):
                savepoint_span = nested_client._savepoint_span
                if (
                    nested_client._savepoint_pending
                    and savepoint_span is not None
                    and current_span.is_within(savepoint_span)
                ):
                    yield nested_client
