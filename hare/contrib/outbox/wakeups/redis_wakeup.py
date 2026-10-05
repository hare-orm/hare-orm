from __future__ import annotations

from typing import Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.wakeups.constants import DEFAULT_WAKEUP_CHANNEL, WAKEUP_TOPIC_SEPARATOR
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.exceptions import ConfigurationError

try:
    import redis.asyncio as redis_asyncio
except ImportError:  # pragma: nocoverage
    redis_asyncio = None  # type: ignore[assignment]


class RedisWakeup(OutboxWakeup):
    """Wakes the relays through Redis pub/sub - a message naming the topics on ``channel``. Needs the
    ``redis`` extra.

    Args:
        url: The Redis URL (``redis://localhost:6379/0``) - or give ``client``.
        client: A ``redis.asyncio`` client of your own, left open.
        channel: The pub/sub channel.

    Raises:
        ConfigurationError: Neither or both of ``url`` and ``client``, an empty ``channel``, or the
            ``redis`` package isn't installed.
    """

    #: The ``redis.asyncio`` package, None without the ``redis`` extra.
    redis: Any = redis_asyncio

    def __init__(self, url: str | None = None, *, client: Any = None, channel: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        BrokerArguments.require_package(self.redis, "RedisWakeup", "redis", "redis")
        if (url is None) == (client is None):
            raise ConfigurationError("RedisWakeup takes either url or client")
        BrokerArguments.require_text("channel", channel)
        self.url = url
        self.channel = channel
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

    async def signal(self, topics: frozenset[str]) -> None:
        await self.get_client().publish(self.channel, WAKEUP_TOPIC_SEPARATOR.join(sorted(topics)))

    async def listen(self, connection_alias: str) -> None:
        pubsub = self.get_client().pubsub()
        try:
            await pubsub.subscribe(self.channel)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data") or b""
                text = data.decode() if isinstance(data, bytes) else str(data)
                self.dispatch(frozenset(topic for topic in text.split(WAKEUP_TOPIC_SEPARATOR) if topic))
        finally:
            await pubsub.aclose()
        raise ConnectionError("the Redis subscription ended")

    async def close(self) -> None:
        if self.owns_client and self.client is not None:
            client, self.client = self.client, None
            await client.aclose()
