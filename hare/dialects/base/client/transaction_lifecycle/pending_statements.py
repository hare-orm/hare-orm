from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span

if TYPE_CHECKING:  # pragma: nocoverage
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
            # Shielded for the reason PendingStatements.send_pending_begin() gives.
            await TransactionEnding.run_shielded_from_cancellation(
                top_level_transaction._driver_send_begin(), on_landed=top_level_transaction._mark_begin_sent
            )
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
            await TransactionEnding.run_shielded_from_cancellation(
                top_level_transaction._driver_send_begin(), on_landed=top_level_transaction._mark_begin_sent
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
        current_span = current_savepoint_span.get()
        if current_span is None:
            return
        # Only the savepoints the calling context runs in - a statement without a span of its own
        # (a stream's fetch) may run beside a sibling's savepoint.
        for nested_client in list(client._get_top_level_transaction()._pending_savepoints):
            savepoint_span = nested_client._savepoint_span
            if (
                nested_client._savepoint_pending
                and savepoint_span is not None
                and current_span.is_within(savepoint_span)
            ):
                await TransactionEnding.run_shielded_from_cancellation(
                    nested_client._driver_savepoint(cast("str", nested_client._savepoint_name)),
                    on_landed=nested_client._drop_pending_savepoint,
                )
