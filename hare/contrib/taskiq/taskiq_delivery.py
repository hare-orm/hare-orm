from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from taskiq.kicker import AsyncKicker

from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.outbox_event import OutboxEvent
from hare.contrib.outbox.relay.outbox_relay import OutboxRelay
from hare.contrib.outbox.wakeups.in_process_wakeup import InProcessWakeup
from hare.contrib.taskiq.constants import (
    DEFAULT_TASKIQ_OUTBOX_TOPIC,
    OUTBOX_ARGS_KEY,
    OUTBOX_KWARGS_KEY,
    OUTBOX_LABELS_KEY,
    OUTBOX_TASK_NAME_KEY,
)
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from taskiq import AsyncBroker
    from taskiq.decor import AsyncTaskiqDecoratedTask

    from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup

TOutboxEvent = TypeVar("TOutboxEvent", bound=OutboxEvent)


class TaskiqDelivery(OutboxDelivery, Generic[TOutboxEvent]):
    """Sends taskiq tasks through the transactional outbox: ``kiq_on_commit()`` writes a task as an
    outbox event - in the current transaction, so it is sent only once the transaction commits and
    never for one rolled back; outside a transaction it commits at once - and the relay of
    ``get_relay()`` sends it, woken right after the commit by the delivery's wakeup. Delivery is at
    least once - a task may arrive twice, so make tasks safe to repeat.

    Run the relay in the process sending the tasks: the default ``InProcessWakeup`` wakes the relays
    of its own process; give a broker's wakeup to send from another one at once.

    Args:
        broker: The broker the tasks are sent through.
        model: The concrete ``OutboxEvent`` model the tasks are written to.
        topic: The outbox topic of the tasks - the relay delivers only these events.
        using: The connection the tasks are written on - the model's when None.
        wakeup: The wakeup of the relay - an ``InProcessWakeup`` of its own by default.

    Raises:
        ConfigurationError: ``topic`` isn't a non-empty string.
    """

    def __init__(
        self,
        broker: AsyncBroker,
        *,
        model: type[TOutboxEvent],
        topic: str = DEFAULT_TASKIQ_OUTBOX_TOPIC,
        using: str | None = None,
        wakeup: OutboxWakeup | None = None,
    ) -> None:
        if not isinstance(topic, str) or not topic:
            raise ConfigurationError(f"topic must be a non-empty string, got {topic!r}")
        self.broker = broker
        self.model = model
        self.topic = topic
        self.using = using
        self.wakeup = wakeup if wakeup is not None else InProcessWakeup()

    async def kiq_on_commit(
        self,
        task: AsyncTaskiqDecoratedTask[Any, Any] | str,
        *args: Any,
        labels: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> TOutboxEvent:
        """Sends a task once the current transaction commits; outside a transaction, at once -
        through the outbox either way.

        Args:
            task: The task, or its name.
            args: Its positional arguments - JSON values.
            labels: Its labels.
            kwargs: Its keyword arguments - JSON values.

        Returns:
            The outbox event of the task.
        """
        payload = {
            OUTBOX_TASK_NAME_KEY: task if isinstance(task, str) else task.task_name,
            OUTBOX_ARGS_KEY: list(args),
            OUTBOX_KWARGS_KEY: dict(kwargs),
            OUTBOX_LABELS_KEY: dict(labels or {}),
        }
        return await self.model.enqueue(self.topic, payload, using=self.using, wakeup=self.wakeup)

    async def deliver(self, event: OutboxEvent) -> None:
        payload = event.payload
        kicker: AsyncKicker[Any, Any] = AsyncKicker(
            task_name=payload[OUTBOX_TASK_NAME_KEY], broker=self.broker, labels=dict(payload[OUTBOX_LABELS_KEY])
        )
        await kicker.kiq(*payload[OUTBOX_ARGS_KEY], **payload[OUTBOX_KWARGS_KEY])

    def get_relay(self, **relay_options: Any) -> OutboxRelay[TOutboxEvent]:
        """The relay sending the tasks - run it with ``async with``, or ``start()``/``stop()``.

        Args:
            relay_options: ``OutboxRelay`` options.

        Returns:
            The relay, delivering only this delivery's topic, woken by its wakeup.
        """
        return OutboxRelay(self.model, self, topics=[self.topic], wakeup=self.wakeup, **relay_options)
