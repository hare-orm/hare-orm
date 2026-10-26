from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.deliveries.callable_delivery import CallableDelivery
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.exceptions import DeliveryError

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
        routes: Mapping[str, OutboxDelivery | Callable[[Any], Awaitable[None]]],
        *,
        default: OutboxDelivery | Callable[[Any], Awaitable[None]] | None = None,
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

    async def deliver(self, event: OutboxEvent) -> None:
        await self.get_delivery(event.topic).deliver(event)

    async def deliver_batch(
        self, events: Sequence[OutboxEvent], *, concurrency: int, timeout_seconds: float
    ) -> list[BaseException | None]:
        outcomes: list[BaseException | None] = [None] * len(events)
        positions_by_delivery: dict[int, tuple[OutboxDelivery, list[int]]] = {}
        for position, event in enumerate(events):
            try:
                delivery = self.get_delivery(event.topic)
            except DeliveryError as error:
                outcomes[position] = error
                continue
            positions_by_delivery.setdefault(id(delivery), (delivery, []))[1].append(position)
        groups = list(positions_by_delivery.values())
        group_outcomes = await asyncio.gather(
            *(
                delivery.deliver_batch(
                    [events[position] for position in positions],
                    concurrency=concurrency,
                    timeout_seconds=timeout_seconds,
                )
                for delivery, positions in groups
            )
        )
        for (_delivery, positions), outcomes_of_group in zip(groups, group_outcomes, strict=True):
            for position, outcome in zip(positions, outcomes_of_group, strict=True):
                outcomes[position] = outcome
        return outcomes

    async def close(self) -> None:
        deliveries = {id(delivery): delivery for delivery in (*self.routes.values(), self.default) if delivery}
        for delivery in deliveries.values():
            await delivery.close()
