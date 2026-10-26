from __future__ import annotations

from collections.abc import Collection

from hare.dialects.base.client.database_client import DatabaseClient
from hare.migrations.drift.drift_result import DriftResult
from hare.migrations.drift.shadow.shadow_drift import ShadowDrift
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
    """Compares the live database schema with ``new_state`` for the models of ``target_labels``: the schema
    the models declare is built in a shadow beside the database, both are read by the connection's
    introspector and compared part by part.

    Args:
        connection: The connection to introspect.
        new_state: The current models' state.
        target_labels: The app labels to compare.
        schema: The schema swept for untracked tables - a schema, an attached database or a database of
            the server - the namespace of tables the database has; the connection's default one when None.
        unmanaged_model_states: The ``Meta.managed = False`` models' states - their tables count as
            known.
        journal_tables: The tables of migration journals besides ``hare_migrations`` - a
            ``MigrationRecorder(table_name=...)``'s - known too.

    Returns:
        The operations, untracked tables, untracked columns and column mismatches found.
    """
    return await ShadowDrift(
        connection, new_state, target_labels, schema, unmanaged_model_states, journal_tables
    ).detect()
