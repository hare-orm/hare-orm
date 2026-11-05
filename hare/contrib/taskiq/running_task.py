from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.contrib.application_lifecycle import ApplicationLifecycle
    from hare.core.hare_context import HareContext
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
        hare_context_token: Restores the Hare context active before the task.
        application_token: Restores the active application before the task.
    """

    task_id: str
    query_tags_token: contextvars.Token[dict[str, str] | None]
    running_task_token: contextvars.Token[RunningTask | None] | None = None
    tenant_token: contextvars.Token[Any | None] | None = None
    transactions: TransactionGroup | None = None
    exception: BaseException | None = None
    hare_context_token: contextvars.Token[HareContext | None] | None = None
    application_token: contextvars.Token[ApplicationLifecycle | None] | None = None
