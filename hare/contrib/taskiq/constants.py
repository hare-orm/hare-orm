from __future__ import annotations

#: The task label the worker reads a task's tenant from, and the sender writes it to, by default.
DEFAULT_TENANT_LABEL = "tenant"
#: The query tags naming the task a statement runs for.
TASK_NAME_QUERY_TAG = "task_name"
TASK_ID_QUERY_TAG = "task_id"
#: The label counting how many times a task was sent again after a transaction conflict.
TRANSACTION_RETRIES_LABEL = "hare_transaction_retries"
#: The most times ``transaction_retries`` lets a task run again.
MAX_TRANSACTION_RETRIES = 100
#: The outbox topic of the tasks ``kiq_on_commit()`` sends, by default.
DEFAULT_TASKIQ_OUTBOX_TOPIC = "taskiq"
#: The keys of a task's outbox payload.
OUTBOX_TASK_NAME_KEY = "task_name"
OUTBOX_ARGS_KEY = "args"
OUTBOX_KWARGS_KEY = "kwargs"
OUTBOX_LABELS_KEY = "labels"
