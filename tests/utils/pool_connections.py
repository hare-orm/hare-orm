"""The connections of a PostgreSQL client's pool made to reconnect - its driver's own ``expire_connections()``."""

from typing import Any


async def expire_pool_connections(client: Any) -> None:
    """Has every connection of the client's pool open anew before its next statement - the ones in use when
    they are given back.

    Args:
        client: An asyncpg or rust_pg client with its pool open.
    """
    await client._pool.expire_connections()
