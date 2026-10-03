from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper

if TYPE_CHECKING:
    from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_transaction_client import AsyncpgTransactionClient


class AsyncpgTransactionConnectionWrapper(ConnectionWrapper[asyncpg.Connection]):
    """The connection of an asyncpg transaction - sends the transaction's BEGIN and the SAVEPOINTs
    still pending ahead of a statement, under the transaction's connection lock."""

    __slots__ = ()

    async def __aenter__(self) -> asyncpg.Connection:
        connection = await super().__aenter__()
        client: AsyncpgTransactionClient = self.client  # type: ignore[assignment]  # cast() is a call per statement
        transaction = client._outer_transaction or client
        if transaction._begin_pending or transaction._pending_savepoints:
            await self._send_pending_statements()
        return connection
