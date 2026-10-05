from __future__ import annotations

from collections.abc import Collection

from hare.dialects.base.client.database_client import DatabaseClient
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.drift.drift_result import DriftResult
from hare.migrations.drift.drift_state_builder import DriftStateBuilder
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State


async def detect_drift(
    connection: DatabaseClient,
    new_state: State,
    target_labels: list[str],
    schema: str | None = None,
    unmanaged_model_states: list[ModelState] | None = None,
    journal_tables: Collection[str] = (),
) -> DriftResult:
    """Compares the live database schema with ``new_state`` for the models of ``target_labels``, by the
    diffing ``makemigrations`` uses, with the database as the old state.

    Args:
        connection: The connection to introspect.
        new_state: The current models' state.
        target_labels: The app labels to compare.
        schema: The schema swept for untracked tables - the connection's default schema when None.
            Ignored on SQLite.
        unmanaged_model_states: The ``Meta.managed = False`` models' states - their tables count as
            known.
        journal_tables: The tables of migration journals besides ``hare_migrations`` - a
            ``MigrationRecorder(table_name=...)``'s - known too.

    Returns:
        The operations, untracked tables, untracked columns and column mismatches found.
    """
    db_state, untracked_tables, untracked_columns, mismatched_columns = await DriftStateBuilder.build_database_state(
        connection, new_state, target_labels, schema, unmanaged_model_states, journal_tables
    )
    operations = OperationGenerator(db_state, new_state).generate(app_labels=target_labels)
    return DriftResult(
        operations=operations,
        untracked_tables=untracked_tables,
        untracked_columns=untracked_columns,
        mismatched_columns=mismatched_columns,
    )
