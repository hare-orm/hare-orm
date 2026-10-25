from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent


class CallableDelivery(OutboxDelivery):
    """A delivery that is an async function of the event - what the relay and ``TopicRouter`` make
    of a function given instead of an ``OutboxDelivery``.

    Args:
        function: Sends an event; raising fails it.
    """

    def __init__(self, function: Callable[[Any], Awaitable[None]]) -> None:
        self.function = function

    async def deliver(self, event: OutboxEvent) -> None:
        await self.function(event)

    @staticmethod
    def of(delivery: OutboxDelivery | Callable[[Any], Awaitable[None]]) -> OutboxDelivery:
        """A delivery, or a function made one.

        Args:
            delivery: The delivery or the function.

        Returns:
            The delivery.
        """
        if isinstance(delivery, OutboxDelivery):
            return delivery
        return CallableDelivery(delivery)
