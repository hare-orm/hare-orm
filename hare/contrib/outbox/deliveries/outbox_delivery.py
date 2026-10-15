from __future__ import annotations

import abc
import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.deliveries.constants import EVENT_ID_HEADER, EVENT_TOPIC_HEADER
from hare.contrib.outbox.outbox_json_encoding import OutboxJsonEncoding

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.outbox.outbox_event import OutboxEvent


class OutboxDelivery(abc.ABC):
    """Sends outbox events where they go - a broker, a task queue, an HTTP endpoint. A delivery only
    sends: the relay retries a failed event, counts its attempts and dead-letters it. Delivery is at
    least once, so a receiver deduplicates on the event's ``id`` - every delivery sends it, with the
    topic and the event's ``headers``, as message headers; the body is the payload's JSON.

    A subclass sends one event (``deliver()``) and, when its broker sends a batch better,
    ``deliver_batch()``.
    """

    @abc.abstractmethod
    async def deliver(self, event: OutboxEvent) -> None:
        """Sends one event.

        Args:
            event: The event.

        Raises:
            Exception: The event wasn't sent - the relay tries it again later.
        """

    async def deliver_batch(
        self, events: Sequence[OutboxEvent], *, concurrency: int, timeout_seconds: float
    ) -> list[BaseException | None]:
        """Sends several events - of distinct ordering keys, so in any order. By default each through
        ``deliver()``, up to ``concurrency`` at a time, each within ``timeout_seconds``.

        Args:
            events: The events.
            concurrency: The most events sent at a time.
            timeout_seconds: The longest the sending of one event - of the batch, for a delivery
                sending batches - may take.

        Returns:
            Per event, None when it was sent, else what failed it.
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def deliver_one(event: OutboxEvent) -> None:
            async with semaphore:
                await asyncio.wait_for(self.deliver(event), timeout_seconds)

        results = await asyncio.gather(*(deliver_one(event) for event in events), return_exceptions=True)
        return [result if isinstance(result, BaseException) else None for result in results]

    async def close(self) -> None:
        """Releases the connections the delivery opened - nothing by default."""

    @staticmethod
    def get_body(event: OutboxEvent) -> bytes:
        """The message body of an event.

        Args:
            event: The event.

        Returns:
            The payload's JSON.
        """
        return OutboxJsonEncoding.dumps(event.payload).encode()

    @staticmethod
    def get_headers(event: OutboxEvent) -> dict[str, str]:
        """The message headers of an event - its own headers, a value other than text as JSON, then
        its id and topic.

        Args:
            event: The event.

        Returns:
            The headers.
        """
        headers: dict[str, str] = {}
        for name, value in (event.headers or {}).items():
            headers[name] = value if isinstance(value, str) else json.dumps(value, default=str)
        headers[EVENT_ID_HEADER] = str(event.id)
        headers[EVENT_TOPIC_HEADER] = event.topic
        return headers

    @staticmethod
    def get_key(event: OutboxEvent) -> Any:
        """The message key of an event - its ordering key, so a broker keeps one key's events in one
        partition.

        Args:
            event: The event.

        Returns:
            The key as bytes, None without an ordering key.
        """
        return event.ordering_key.encode() if event.ordering_key else None
