from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder


class NoopRecorder(MigrationRecorder):
    """A recorder that returns no applied migrations and never touches the DB - used by the
    autodetector (diffing state, never actually running migrations) and sqlmigrate (rendering
    SQL for inspection, never applying it)."""

    def __init__(self) -> None:
        super().__init__(connection=None)

    async def applied_migrations(self) -> list[MigrationKey]:
        return []

    async def ensure_schema(self, _schema_editor) -> None:
        return None
