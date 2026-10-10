from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from taskiq import TaskiqEvents

from hare.contrib.application_lifecycle import ApplicationLifecycle
from hare.contrib.taskiq.constants import DEFAULT_TENANT_LABEL
from hare.contrib.taskiq.hare_taskiq_middleware import HareTaskiqMiddleware

if TYPE_CHECKING:  # pragma: nocoverage
    from taskiq import AsyncBroker, TaskiqState

    from hare.core.config import HareConfig


class HareTaskiq:
    """Hare in a taskiq broker's workers: the Hare context opened when a worker starts
    (``WORKER_STARTUP``) and closed when it stops (``WORKER_SHUTDOWN``), and
    ``HareTaskiqMiddleware`` added to the broker - for the application sending tasks too, which
    writes the tenant into their labels.

    Example:
        broker = ListQueueBroker("redis://...")
        hare_taskiq = HareTaskiq(broker, HARE_ORM, atomic_tasks=True, transaction_retries=3)

    Args:
        broker: The broker.
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        atomic_tasks: True for a transaction per task on the default connection, the connection
            names for one on each of them, False for none.
        tenant_label: The label a task's tenant goes in; None to leave tenants alone.
        transaction_retries: How many times a task is sent again after a transaction conflict.

    Raises:
        ConfigurationError: An option has the wrong type or is out of range.
    """

    def __init__(
        self,
        broker: AsyncBroker,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        atomic_tasks: bool | Sequence[str] = False,
        tenant_label: str | None = DEFAULT_TENANT_LABEL,
        transaction_retries: int = 0,
    ) -> None:
        self.broker = broker
        self.middleware = HareTaskiqMiddleware(
            atomic_tasks=atomic_tasks, tenant_label=tenant_label, transaction_retries=transaction_retries
        )
        self.lifecycle = ApplicationLifecycle(config, transactions=atomic_tasks, transactions_option="atomic_tasks")
        broker.add_middlewares(self.middleware)
        broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, self.start)
        broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, self.stop)

    async def start(self, state: TaskiqState) -> None:
        """Opens the worker's Hare context.

        Args:
            state: The broker's state.

        Raises:
            ConfigurationError: ``atomic_tasks`` names a connection the configuration lacks, or the
                context is open already.
        """
        await self.lifecycle.start()

    async def stop(self, state: TaskiqState) -> None:
        """Closes the worker's connections and its Hare context.

        Args:
            state: The broker's state.
        """
        await self.lifecycle.stop()
