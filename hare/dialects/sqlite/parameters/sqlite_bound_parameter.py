from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SqliteBoundParameter:
    """A prepared storage value, isolated from process-wide sqlite3 adapters.

    sqlite3 adapts this wrapper once through its public preparation protocol. The returned
    storage value is bound directly, without another lookup in the global adapter registry.
    """

    value: Any

    def __conform__(self, protocol: type) -> Any:
        """Return the storage value for SQLite's parameter preparation protocol."""
        import sqlite3

        return self.value if protocol is sqlite3.PrepareProtocol else None
