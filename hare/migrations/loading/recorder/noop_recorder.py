from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class NoopRecorder(MigrationRecorder):
    """A recorder that returns no applied migrations and never touches the DB - used by the
    autodetector (diffing state, never actually running migrations) and sqlmigrate (rendering
    SQL for inspection, never applying it)."""

    def __init__(self) -> None:
        # It never touches a database - there is no connection to keep.
        super().__init__(connection=cast("DatabaseClient", None))

    async def applied_migrations(self, connection: object = None) -> list[MigrationKey]:
        return []

    async def ensure_schema(self, _schema_editor: BaseSchemaEditor) -> None:
        return None
