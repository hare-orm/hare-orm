from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DistributedDecision:
    """One durable decision; participant data is read only after checking its coordinator."""

    xid: str
    participant_aliases_json: str
    is_stale: bool

    @property
    def participants(self) -> tuple[str, ...]:
        """Decode participant aliases of a decision belonging to this recovery run."""
        return tuple(json.loads(self.participant_aliases_json))
