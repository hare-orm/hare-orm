from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class MigrationKey:
    app_label: str
    name: str

    def __str__(self) -> str:
        return f"{self.app_label}.{self.name}"
