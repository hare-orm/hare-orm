from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.deliveries.callable_delivery import CallableDelivery
from hare.contrib.outbox.deliveries.delivery_batch import DeliveryBatch
from hare.contrib.outbox.deliveries.delivery_result import DeliveryResult
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.exceptions import DeliveryError
from hare.core.resource_completion import ResourceCompletion

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent


class TopicRouter(OutboxDelivery):
    """Sends each event through the delivery of its topic - a topic named exactly, else the first
    pattern (``shop.order.*``, ``fnmatch`` syntax) it matches in the order given, else ``default``.
    An event of a topic without a delivery fails - it is retried and dead-lettered, never marked
    delivered.

    Args:
        routes: The delivery of each topic or pattern - an ``OutboxDelivery`` or an async function of
            the event.
        default: The delivery of a topic no route names.
    """

    def __init__(
        self,
        routes: Mapping[str, OutboxDelivery | Callable[[Any], Awaitable[DeliveryResult | None]]],
        *,
        default: OutboxDelivery | Callable[[Any], Awaitable[DeliveryResult | None]] | None = None,
    ) -> None:
        self.routes: dict[str, OutboxDelivery] = {
            pattern: CallableDelivery.of(delivery) for pattern, delivery in routes.items()
        }
        self.default = CallableDelivery.of(default) if default is not None else None

    def get_delivery(self, topic: str) -> OutboxDelivery:
        """The delivery of a topic.

        Args:
            topic: The topic.

        Returns:
            The delivery.

        Raises:
            DeliveryError: No route or default takes the topic.
        """
        delivery = self.routes.get(topic)
        if delivery is not None:
            return delivery
        for pattern, pattern_delivery in self.routes.items():
            if fnmatch.fnmatchcase(topic, pattern):
                return pattern_delivery
        if self.default is not None:
            return self.default
        raise DeliveryError(f"no delivery takes topic {topic!r}")

    async def deliver(self, event: OutboxEvent) -> DeliveryResult | None:
        return await self.get_delivery(event.topic).deliver(event)

    async def deliver_batch(
        self, events: Sequence[OutboxEvent], *, concurrency: int, timeout_seconds: float
    ) -> list[DeliveryResult]:
        """Keeps each transport's batching and isolates failures between routes."""
        outcomes: list[DeliveryResult] = []
        groups: dict[int, tuple[OutboxDelivery, list[OutboxEvent]]] = {}
        for event in events:
            try:
                delivery = self.get_delivery(event.topic)
            except DeliveryError as error:
                outcomes.append(DeliveryResult(event.id, error))
                continue
            groups.setdefault(id(delivery), (delivery, []))[1].append(event)
        group_outcomes = await asyncio.gather(
            *(
                DeliveryBatch.deliver(
                    delivery,
                    group,
                    concurrency=concurrency,
                    timeout_seconds=timeout_seconds,
                )
                for delivery, group in groups.values()
            )
        )
        for results in group_outcomes:
            outcomes.extend(results)
        return outcomes

    async def close(self) -> None:
        """Close every distinct transport, retaining failures without skipping later releases."""
        deliveries = {id(delivery): delivery for delivery in (*self.routes.values(), self.default) if delivery}
        releases = AsyncExitStack()
        for delivery in reversed(tuple(deliveries.values())):
            releases.push_async_callback(delivery.close)
        await ResourceCompletion.wait(releases.aclose())
