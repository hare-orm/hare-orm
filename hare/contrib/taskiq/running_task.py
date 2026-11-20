from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.contrib.application_lifecycle import ApplicationLifecycleTokens
    from hare.transactions.transaction_group import TransactionGroup


@dataclass
class RunningTask:
    """What ``HareTaskiqMiddleware`` set up for the task running in this context, to undo once it
    ends.

    Attributes:
        task_id: The task's id.
        query_tags_token: Restores the query tags active before the task.
        running_task_token: Restores the running task of the context before - a task run in place
            from another one.
        tenant_token: Restores the tenant scope active before the task; None when the task set none.
        transactions: The task's open transactions; None without ``atomic_tasks``.
        exception: What the task raised, None while it hasn't.
        lifecycle_tokens: Restores the application's context and active lifecycle together;
            None when the task activated no application.
    """

    task_id: str
    query_tags_token: contextvars.Token[dict[str, str] | None]
    running_task_token: contextvars.Token[RunningTask | None] | None = None
    tenant_token: contextvars.Token[Any | None] | None = None
    transactions: TransactionGroup | None = None
    exception: BaseException | None = None
    lifecycle_tokens: ApplicationLifecycleTokens | None = None
