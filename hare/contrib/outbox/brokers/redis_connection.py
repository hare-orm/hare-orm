from __future__ import annotations

from typing import Any

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None  # type: ignore[assignment]


class RedisConnection:
    """The Redis client of a delivery or a wakeup - made of a URL on first use and closed with it, or
    one given by the application and left open.

    Args:
        url: The Redis URL - or give ``client``.
        client: A ``redis.asyncio`` client of the application.
    """

    #: The ``redis.asyncio`` package, None without the ``redis`` extra.
    redis: Any = redis_asyncio

    def __init__(self, url: str | None, client: Any) -> None:
        self.url = url
        self.owns_client = client is None
        self.client: Any = client

    def get_client(self) -> Any:
        """The client, connected on first use.

        Returns:
            The ``redis.asyncio`` client.
        """
        if self.client is None:
            self.client = self.redis.from_url(self.url)
        return self.client

    async def close(self) -> None:
        """Closes a client made of the URL; one given by the application stays open."""
        if self.owns_client and self.client is not None:
            client, self.client = self.client, None
            await client.aclose()
