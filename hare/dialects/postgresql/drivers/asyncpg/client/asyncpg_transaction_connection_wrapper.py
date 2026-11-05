from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient


class AsyncpgTransactionConnectionWrapper(ConnectionWrapper[asyncpg.Connection]):
    """Refresh codecs on pinned connections under the same lock as user statements."""

    client: AsyncpgTransactionClient

    def __init__(self, client: AsyncpgTransactionClient) -> None:
        super().__init__(client._lock, client)

    async def ensure_connection(self) -> None:
        await super().ensure_connection()
        client = self.client
        await PendingStatements.send_pending_transaction_statements(client)
        transaction = client._outer_transaction or client
        await transaction._parent.refresh_schema_codecs(self.connection, transaction._held_pool)
