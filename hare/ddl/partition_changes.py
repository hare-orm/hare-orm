from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PartitionChanges:
    """Partition changes shared by schema planning, migration generation and drift."""

    removed: tuple[Any, ...]
    renamed: tuple[tuple[Any, Any], ...]
    added: tuple[Any, ...]

    @classmethod
    def compare(cls, old: dict[str, Any], new: dict[str, Any]) -> PartitionChanges:
        removed = {name: partition for name, partition in old.items() if new.get(name) != partition}
        added = {name: partition for name, partition in new.items() if old.get(name) != partition}
        renamed = []
        for name, partition in list(removed.items()):
            matches = [
                (new_name, replacement)
                for new_name, replacement in added.items()
                if (new_name == name or new_name not in old) and partition.has_same_bound(replacement)
            ]
            if len(matches) == 1:
                new_name, replacement = matches[0]
                renamed.append((partition, replacement))
                del removed[name]
                del added[new_name]
        return cls(tuple(removed.values()), tuple(renamed), tuple(added.values()))
