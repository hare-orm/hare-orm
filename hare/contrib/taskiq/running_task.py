from __future__ import annotations

import contextvars
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any


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
    """

    task_id: str
    query_tags_token: contextvars.Token[dict[str, str] | None]
    running_task_token: contextvars.Token[RunningTask | None] | None = None
    tenant_token: contextvars.Token[Any | None] | None = None
    transactions: AsyncExitStack | None = None
    exception: BaseException | None = None
