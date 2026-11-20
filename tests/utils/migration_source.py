from __future__ import annotations

from typing import Any

from hare.migrations.migration import Migration


class MigrationSource:
    """Read a migration declaration generated in memory by a test."""

    @staticmethod
    def load(source: str, name: str, app_label: str, *, filename: str = "<migration>") -> Migration:
        """Instantiate the migration written in the supplied test source."""
        namespace: dict[str, Any] = {}
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102  # nosec B102
        migration: Migration = namespace["Migration"](name, app_label)
        return migration
