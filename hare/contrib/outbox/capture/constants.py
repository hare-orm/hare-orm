from __future__ import annotations

#: The topic of a captured change by default - lowercase, the operation in the past tense.
DEFAULT_CHANGE_TOPIC_TEMPLATE = "{app}.{model}.{operation}"

#: The ordering key of a captured change by default - its row's.
DEFAULT_CHANGE_ORDERING_KEY_TEMPLATE = "{label}:{pk}"

#: The name of each operation in a captured change's topic and envelope, keyed by ``RowOperation``
#: values.
CHANGE_OPERATION_NAMES: dict[str, str] = {"insert": "inserted", "update": "updated", "delete": "deleted"}

#: The keys of a captured change's envelope - ``ChangeExtension.payload`` may not set them.
CHANGE_ENVELOPE_KEYS = frozenset({"model", "operation", "pk", "changed", "before", "after", "occurred_at"})
