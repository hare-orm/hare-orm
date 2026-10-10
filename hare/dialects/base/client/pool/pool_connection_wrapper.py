from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.database_client import DatabaseClient

TConnection = TypeVar("TConnection")  # Instance of client connection, such as: asyncpg.Connection()


class PoolConnectionWrapper(Generic[TConnection]):
    """Class to manage acquiring from and releasing connections to a pool."""

    __slots__ = ("client", "connection", "pool", "_pool_init_lock")

    def __init__(self, client: DatabaseClient, pool_init_lock: asyncio.Lock) -> None:
        self.client = client
        self.connection: TConnection | None = None
        self.pool: Any = None
        self._pool_init_lock = pool_init_lock

    async def ensure_connection(self) -> None:
        if not self.client._pool:
            # a safeguard against multiple concurrent tasks trying to initialize the pool
            async with self._pool_init_lock:
                if not self.client._pool:
                    await self.client.create_connection(with_db=True)

    async def __aenter__(self) -> TConnection:
        await self.ensure_connection()
        # get first available connection. If none available, wait until one is released
        self.pool = self.client._pool
        connection: TConnection = await self.client._pool_acquire()
        self.connection = connection
        return connection

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        # Back to the pool it was acquired from - the client's own pool may have been closed
        # (and cleared) by another task in the meantime.
        await self.client._pool_release(self.pool, self.connection)
