from __future__ import annotations

import contextvars
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from taskiq import TaskiqMiddleware
from taskiq.exceptions import NoResultError
from taskiq.kicker import AsyncKicker

from hare.contrib.application_lifecycle import ApplicationLifecycle
from hare.contrib.taskiq.constants import (
    DEFAULT_TENANT_LABEL,
    MAX_TRANSACTION_RETRIES,
    TASK_ID_QUERY_TAG,
    TASK_NAME_QUERY_TAG,
    TRANSACTION_RETRIES_LABEL,
)
from hare.contrib.taskiq.running_task import RunningTask
from hare.core.log import logger
from hare.exceptions import ConfigurationError, TransactionRetryError
from hare.instrumentation.queries.query_tags import QueryTags
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions.transactions import Transactions

if TYPE_CHECKING:  # pragma: nocoverage
    from taskiq import TaskiqMessage, TaskiqResult


class HareTaskiqMiddleware(TaskiqMiddleware):
    """Runs every task the way a Hare application expects: its statements tagged with the task's
    name and id (``QueryTags``), the tenant scope taken from its label (``Tenancy.scope``), and with
    ``atomic_tasks`` inside a transaction committed when the task returns and rolled back when it
    raises. A task whose transaction hits a conflict - ``TransactionRetryError``: a serialization
    failure, a deadlock - is sent again under its own id, up to ``transaction_retries`` times.

    Sending a task, the middleware writes the tenant scope active then into the tenant label - a
    single tenant value (a string, an integer, a UUID); the label already set is kept.

    Args:
        atomic_tasks: True for a transaction per task on the default connection, the connection
            names for one on each of them, False for none.
        tenant_label: The label the tenant goes in; None to leave tenants alone.
        transaction_retries: How many times a task is sent again after a transaction conflict, in
            ``0..MAX_TRANSACTION_RETRIES``.

    Raises:
        ConfigurationError: An option has the wrong type or is out of range.
    """

    #: What the middleware set up for the task running in this context.
    running_task: ClassVar[contextvars.ContextVar[RunningTask | None]] = contextvars.ContextVar(
        "hare_taskiq_running_task", default=None
    )

    def __init__(
        self,
        *,
        atomic_tasks: bool | Sequence[str] = False,
        tenant_label: str | None = DEFAULT_TENANT_LABEL,
        transaction_retries: int = 0,
    ) -> None:
        super().__init__()
        if tenant_label is not None and (not isinstance(tenant_label, str) or not tenant_label):
            raise ConfigurationError(f"tenant_label must be None or a non-empty label name, got {tenant_label!r}")
        if (
            isinstance(transaction_retries, bool)
            or not isinstance(transaction_retries, int)
            or not 0 <= transaction_retries <= MAX_TRANSACTION_RETRIES
        ):
            raise ConfigurationError(
                f"transaction_retries must be an int in 0..{MAX_TRANSACTION_RETRIES}, got {transaction_retries!r}"
            )
        self.transaction_connection_names = ApplicationLifecycle.get_transaction_connection_names(
            atomic_tasks, "atomic_tasks"
        )
        self.tenant_label = tenant_label
        self.transaction_retries = transaction_retries

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        if self.tenant_label is None or self.tenant_label in message.labels:
            return message
        tenant = Tenancy.current.get()
        if isinstance(tenant, UUID):
            message.labels[self.tenant_label] = str(tenant)
        elif isinstance(tenant, (str, int)) and not isinstance(tenant, bool):
            message.labels[self.tenant_label] = tenant
        return message

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        tags = {
            **(QueryTags.current.get() or {}),
            TASK_NAME_QUERY_TAG: message.task_name,
            TASK_ID_QUERY_TAG: message.task_id,
        }
        running_task = RunningTask(task_id=message.task_id, query_tags_token=QueryTags.set(tags))
        running_task.running_task_token = self.running_task.set(running_task)
        try:
            tenant = message.labels.get(self.tenant_label) if self.tenant_label is not None else None
            if tenant is not None:
                running_task.tenant_token = Tenancy.set(tenant)
            if self.transaction_connection_names:
                transactions = AsyncExitStack()
                try:
                    for connection_alias in self.transaction_connection_names:
                        await transactions.enter_async_context(Transactions.atomic(using=connection_alias))
                except BaseException:
                    await transactions.aclose()
                    raise
                running_task.transactions = transactions
        except BaseException:
            self.finish(running_task)
            raise
        return message

    def on_error(self, message: TaskiqMessage, result: TaskiqResult[Any], exception: BaseException) -> None:
        running_task = self.running_task.get()
        if running_task is not None and running_task.task_id == message.task_id:
            running_task.exception = exception

    async def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        running_task = self.running_task.get()
        if running_task is None or running_task.task_id != message.task_id:
            return
        exception = running_task.exception
        try:
            if running_task.transactions is not None:
                try:
                    if exception is None:
                        await running_task.transactions.__aexit__(None, None, None)
                    else:
                        await running_task.transactions.__aexit__(type(exception), exception, exception.__traceback__)
                except Exception as transaction_error:
                    # The commit failed - the task failed with it.
                    exception = transaction_error
                    result.is_err = True
                    result.error = transaction_error
        finally:
            self.finish(running_task)
        if isinstance(exception, TransactionRetryError):
            await self.send_again(message, result)

    def finish(self, running_task: RunningTask) -> None:
        """Restores what the task's context had before it.

        Args:
            running_task: What the middleware set up for the task.
        """
        if running_task.running_task_token is not None:
            self.running_task.reset(running_task.running_task_token)
        if running_task.tenant_token is not None:
            Tenancy.reset(running_task.tenant_token)
        QueryTags.reset(running_task.query_tags_token)

    async def send_again(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        """Sends a task that hit a transaction conflict again, under its own id - its failed
        result isn't saved then - while ``transaction_retries`` allows.

        Args:
            message: The task's message.
            result: Its result.
        """
        retries = int(message.labels.get(TRANSACTION_RETRIES_LABEL, 0))
        if retries >= self.transaction_retries:
            if self.transaction_retries:
                logger.warning(
                    "Task %s (%s) hit a transaction conflict again after %d retries - giving up",
                    message.task_name,
                    message.task_id,
                    retries,
                )
            return
        kicker: AsyncKicker[Any, Any] = (
            AsyncKicker(task_name=message.task_name, broker=self.broker, labels=dict(message.labels))
            .with_task_id(message.task_id)
            .with_labels(**{TRANSACTION_RETRIES_LABEL: retries + 1})
        )
        logger.info("Task %s (%s) hit a transaction conflict - sending it again", message.task_name, message.task_id)
        await kicker.kiq(*message.args, **message.kwargs)
        result.error = NoResultError()
