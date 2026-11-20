from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.contrib.outbox.deliveries.delivery_result import DeliveryResult
from hare.contrib.outbox.deliveries.messages import INVALID_BATCH_RESULTS
from hare.contrib.outbox.exceptions import DeliveryError

if TYPE_CHECKING:
    from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
    from hare.contrib.outbox.outbox_event import OutboxEvent


class DeliveryBatch:
    """Runs a transport batch and checks event identity before any outcome is persisted.

    A transport exception or malformed response fails only this transport's batch.
    Successful sibling batches routed elsewhere retain their acknowledgements.
    Cancellation propagates to the caller.
    """

    @staticmethod
    def check(events: Sequence[OutboxEvent], results: Sequence[DeliveryResult]) -> None:
        """Requires exactly one result for every input event, in any order."""
        event_ids = {event.id for event in events}
        if (
            len(results) != len(events)
            or {result.event_id for result in results} != event_ids
            or len(event_ids) != len(events)
        ):
            raise DeliveryError(INVALID_BATCH_RESULTS)

    @staticmethod
    async def deliver(
        delivery: OutboxDelivery, events: Sequence[OutboxEvent], *, concurrency: int, timeout_seconds: float
    ) -> list[DeliveryResult]:
        try:
            results = await delivery.deliver_batch(events, concurrency=concurrency, timeout_seconds=timeout_seconds)
            DeliveryBatch.check(events, results)
        except Exception as error:
            return [DeliveryResult(event.id, error) for event in events]
        return results
