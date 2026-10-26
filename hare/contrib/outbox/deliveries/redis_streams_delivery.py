from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.brokers.redis_connection import RedisConnection
from hare.contrib.outbox.deliveries.constants import EVENT_ID_HEADER, REDIS_STREAM_EVENT_FIELD
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.outbox_json_encoding import OutboxJsonEncoding
from hare.contrib.package_checks import PackageChecks
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent


class RedisStreamsDelivery(OutboxDelivery):
    """Sends events to Redis streams - an ``XADD`` to ``stream``, or to the stream named like the
    event's topic, of one entry: the event's id, topic, ordering key, headers and payload as JSON
    under ``REDIS_STREAM_EVENT_FIELD``. A batch is sent through one pipeline. Needs the ``redis``
    extra.

    Args:
        url: The Redis URL (``redis://localhost:6379/0``) - or give ``client``.
        client: A ``redis.asyncio`` client of your own, left open.
        stream: The stream of every event; None for each event's own topic.
        max_length: Trim each stream to about this many entries (``MAXLEN ~``); None keeps all.

    Raises:
        ConfigurationError: Neither or both of ``url`` and ``client``, or the ``redis`` package isn't
            installed.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any = None,
        stream: str | None = None,
        max_length: int | None = None,
    ) -> None:
        PackageChecks.require_package(RedisConnection.redis, "RedisStreamsDelivery", "redis", "redis")
        if (url is None) == (client is None):
            raise ConfigurationError("RedisStreamsDelivery takes either url or client")
        self.stream = stream
        self.max_length = max_length
        self.broker_connection = RedisConnection(url, client)

    def get_entry(self, event: OutboxEvent) -> dict[str, str]:
        """The stream entry of an event.

        Args:
            event: The event.

        Returns:
            The entry's fields.
        """
        document = {
            "id": str(event.id),
            "topic": event.topic,
            "ordering_key": event.ordering_key,
            "headers": event.headers or {},
            "payload": event.payload,
        }
        return {EVENT_ID_HEADER: str(event.id), REDIS_STREAM_EVENT_FIELD: OutboxJsonEncoding.dumps(document)}

    def add_entry(self, target: Any, event: OutboxEvent) -> Any:
        """Adds an event's entry through a client or a pipeline.

        Args:
            target: The client or the pipeline.
            event: The event.

        Returns:
            What the ``XADD`` returns.
        """
        return target.xadd(
            self.stream or event.topic,
            self.get_entry(event),
            maxlen=self.max_length,
            approximate=self.max_length is not None,
        )

    async def deliver(self, event: OutboxEvent) -> None:
        await self.add_entry(self.broker_connection.get_client(), event)

    async def deliver_batch(
        self, events: Sequence[OutboxEvent], *, concurrency: int, timeout_seconds: float
    ) -> list[BaseException | None]:
        pipeline = self.broker_connection.get_client().pipeline(transaction=False)
        for event in events:
            self.add_entry(pipeline, event)
        try:
            results = await asyncio.wait_for(pipeline.execute(raise_on_error=False), timeout_seconds)
        except Exception as error:
            return [error] * len(events)
        return [result if isinstance(result, BaseException) else None for result in results]

    async def close(self) -> None:
        await self.broker_connection.close()
