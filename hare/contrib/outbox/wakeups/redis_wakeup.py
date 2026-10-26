from __future__ import annotations

from typing import Any

from hare.contrib.outbox.brokers.redis_connection import RedisConnection
from hare.contrib.outbox.wakeups.constants import DEFAULT_WAKEUP_CHANNEL, WAKEUP_TOPIC_SEPARATOR
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.contrib.package_checks import PackageChecks
from hare.exceptions import ConfigurationError


class RedisWakeup(OutboxWakeup):
    """Wakes the relays through Redis pub/sub - a message naming the topics on ``channel``. Needs the
    ``redis`` extra.

    Args:
        url: The Redis URL (``redis://localhost:6379/0``) - or give ``client``.
        client: A ``redis.asyncio`` client of your own, left open.
        channel: The pub/sub channel.

    Raises:
        ConfigurationError: Neither or both of ``url`` and ``client``, or the ``redis`` package isn't
            installed.
    """

    def __init__(self, url: str | None = None, *, client: Any = None, channel: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        PackageChecks.require_package(RedisConnection.redis, "RedisWakeup", "redis", "redis")
        if (url is None) == (client is None):
            raise ConfigurationError("RedisWakeup takes either url or client")
        self.channel = channel
        self.broker_connection = RedisConnection(url, client)

    async def signal(self, topics: frozenset[str]) -> None:
        await self.broker_connection.get_client().publish(self.channel, WAKEUP_TOPIC_SEPARATOR.join(sorted(topics)))

    async def listen(self, connection_alias: str) -> None:
        pubsub = self.broker_connection.get_client().pubsub()
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
        await self.broker_connection.close()
