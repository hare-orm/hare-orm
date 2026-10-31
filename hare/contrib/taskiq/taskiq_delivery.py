from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

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
from hare.contrib.taskiq.hare_taskiq_middleware import HareTaskiqMiddleware
from hare.core.hare_context import HareContext

if TYPE_CHECKING:
    from taskiq import AsyncBroker
    from taskiq.decor import AsyncTaskiqDecoratedTask

    from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
    from hare.fields.relations.swappable_model_reference import SwappableModelReference

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
        model: The concrete ``OutboxEvent`` model the tasks are written to - its class, its
            ``"app.Model"`` label or a ``swappable()`` setting; a label is looked up in the current context
            when the model is first needed.
        topic: The outbox topic of the tasks - the relay delivers only these events.
        using: The connection the tasks are written on - the model's when None.
        wakeup: The wakeup of the relay - an ``InProcessWakeup`` of its own by default.

    """

    def __init__(
        self,
        broker: AsyncBroker,
        *,
        model: type[TOutboxEvent] | str | SwappableModelReference,
        topic: str = DEFAULT_TASKIQ_OUTBOX_TOPIC,
        using: str | None = None,
        wakeup: OutboxWakeup | None = None,
    ) -> None:
        self.broker = broker
        #: The model as it was given - a label is looked up on first use.
        self.model_reference = model
        self._model: type[TOutboxEvent] | None = model if isinstance(model, type) else None
        self.topic = topic
        self.using = using
        self.wakeup = wakeup if wakeup is not None else InProcessWakeup()

    @property
    def model(self) -> type[TOutboxEvent]:
        """The ``OutboxEvent`` model - a label given looked up in the current context once.

        Raises:
            ConfigurationError: The label names no model of the current context.
        """
        if self._model is None:
            self._model = cast("type[TOutboxEvent]", HareContext.require_current().get_model(self.model_reference))
        return self._model

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
        task_labels = dict(labels or {})
        # The tenant scope of the sender, not of the relay sending the task later.
        for middleware in self.broker.middlewares:
            if isinstance(middleware, HareTaskiqMiddleware):
                middleware.add_tenant_label(task_labels)
        payload = {
            OUTBOX_TASK_NAME_KEY: task if isinstance(task, str) else task.task_name,
            OUTBOX_ARGS_KEY: list(args),
            OUTBOX_KWARGS_KEY: dict(kwargs),
            OUTBOX_LABELS_KEY: task_labels,
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
        return OutboxRelay(self.model_reference, self, topics=[self.topic], wakeup=self.wakeup, **relay_options)
