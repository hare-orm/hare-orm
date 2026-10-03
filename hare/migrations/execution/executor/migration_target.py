from dataclasses import dataclass


@dataclass(frozen=True)
class MigrationTarget:
    app_label: str
    name: str
