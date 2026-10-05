from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.base.client.database_client import DatabaseClient

TConnection = TypeVar("TConnection")  # Instance of client connection, such as: asyncpg.Connection()


class PoolConnectionWrapper(Generic[TConnection]):
    """Class to manage acquiring from and releasing connections to a pool."""

    __slots__ = ("client", "connection", "pool")

    def __init__(self, client: DatabaseClient) -> None:
        self.client = client
        self.connection: TConnection | None = None
        self.pool: Any = None

    async def __aenter__(self) -> TConnection:
        # The first available connection - waited for when none is idle.
        self.pool, connection = await self.client.acquire_from_open_pool()
        self.connection = connection
        return cast("TConnection", connection)

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        # Back to the pool it was acquired from - the client's own pool may have been closed
        # (and cleared) by another task in the meantime.
        await self.client._pool_release(self.pool, self.connection)
