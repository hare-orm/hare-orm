from __future__ import annotations

#: The attributes of a model state kept as dicts telling the state change journal of their changes.
TRACKED_MODEL_STATE_ATTRIBUTES = frozenset({"fields", "options"})
