"""Runs one migration on one connection: applies or unapplies it in a transaction and records it,
renders its SQL without running it, and tells what its operations do to the data - for migration
files and for migrations an application builds in memory or keeps in its database alike."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.config.migrations_config import MigrationsConfig
from hare.core.connections.connections import Connections
from hare.migrations.exceptions import PartiallyAppliedMigrationError
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.transactions.transactions import Transactions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
    from hare.migrations.migration import Migration
    from hare.migrations.reports.operation_effect import OperationEffect
    from hare.migrations.state.state import State


class MigrationRunner:
    """Applies and unapplies migrations on a connection.

    An atomic migration (``Migration.atomic``, the default) runs in one transaction with its
    record in the journal, where the database runs DDL in a transaction; a non-atomic one runs
    operation by operation and raises ``PartiallyAppliedMigrationError`` when one fails partway.
    Constraint checks are deferred while it runs and checked before the commit.

    Args:
        connection: The connection the migrations run on.
        recorder: The journal of applied migrations - ``MigrationRecorder(connection)`` for the
            one ``hare migrate`` keeps, one on a table of its own for migrations an application
            keeps elsewhere; None records nothing.
        lock_timeout: Seconds a statement of a migration may wait for a lock another session holds
            before the migration fails - None to wait as long as the database does. An atomic
            migration's transaction carries it; a non-atomic one runs on a connection of its own
            with it (``DatabaseClient.lock_timeout_session()``).

    Raises:
        ConfigurationError: ``lock_timeout`` isn't a number of seconds within the supported range.
    """

    def __init__(
        self,
        connection: DatabaseClient,
        *,
        recorder: MigrationRecorder | None = None,
        lock_timeout: float | None = None,
    ) -> None:
        self.connection = connection
        self.recorder = recorder
        self.lock_timeout = MigrationsConfig(lock_timeout=lock_timeout).lock_timeout

    def get_schema_editor(self, *, atomic: bool = True, collect_sql: bool = False) -> BaseSchemaEditor:
        """A schema editor of the connection's dialect.

        Args:
            atomic: Whether the migration it runs is atomic.
            collect_sql: Collect the SQL instead of running it.

        Returns:
            The schema editor.
        """
        editor_class = self.connection.dialect.schema_editor_class
        return editor_class(self.connection, atomic=atomic, collect_sql=collect_sql)

    async def ensure_journal(self) -> None:
        """Creates the recorder's table when it doesn't exist yet - nothing without a recorder."""
        if self.recorder is not None:
            await self.recorder.ensure_schema(self.get_schema_editor(atomic=False))

    @asynccontextmanager
    async def migration_schema_editor(self, migration: Migration) -> AsyncGenerator[BaseSchemaEditor]:
        """The schema editor a migration runs with - on a connection of its own with the lock
        timeout when the migration runs outside a transaction; every query of the connection's
        name inside the block goes to that connection.

        Args:
            migration: The migration.

        Returns:
            The context giving the schema editor.
        """
        schema_editor = self.get_schema_editor(atomic=migration.atomic)
        if self.lock_timeout is None or schema_editor.atomic_migration or schema_editor.collect_sql:
            yield schema_editor
            return
        async with self.connection.lock_timeout_session(self.lock_timeout) as session_client:
            connection_token = Connections.current().set(session_client.connection_alias, session_client)
            try:
                schema_editor.client = session_client
                yield schema_editor
            finally:
                Connections.current().reset(connection_token)

    async def apply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State:
        """Applies a migration and records it - see ``_run()``.

        Args:
            migration: The migration.
            state: The state before it - brought forward in place.
            dry_run: Run the operations without touching the database or the journal.

        Returns:
            The state after it.
        """
        async with self.migration_schema_editor(migration) as schema_editor:
            return await self._run(migration, state, schema_editor, forwards=True, dry_run=dry_run)

    async def unapply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State:
        """Unapplies a migration and removes its record - see ``_run()``.

        Args:
            migration: The migration.
            state: The state before the migration was applied.
            dry_run: Run the operations without touching the database or the journal.

        Returns:
            ``state``, the state once it is unapplied.
        """
        async with self.migration_schema_editor(migration) as schema_editor:
            return await self._run(migration, state, schema_editor, forwards=False, dry_run=dry_run)

    async def _run(
        self,
        migration: Migration,
        state: State,
        schema_editor: BaseSchemaEditor,
        *,
        forwards: bool,
        dry_run: bool = False,
    ) -> State:
        """Applies or unapplies a migration and records it.

        Args:
            migration: The migration.
            state: The state before it - brought forward in place when applying.
            schema_editor: The schema editor it runs with.
            forwards: True to apply it, False to unapply it.
            dry_run: Run the operations without touching the database or the journal.

        Returns:
            The state after it.

        Raises:
            IrreversibleMigrationError: An operation can't be unapplied.
            PartiallyAppliedMigrationError: An operation of a non-atomic migration failed after
                others had run; its record is left as it was.
        """
        # Entered on schema_editor.client as it is before the transaction below opens it - SQLite
        # no-ops a PRAGMA foreign_keys change made while a transaction is active.
        async with schema_editor.constraint_checking_disabled():
            if schema_editor.atomic_migration:
                # Restored once the transaction commits, before constraint_checking_disabled()'s
                # own cleanup runs on it - not on the finished transaction's client.
                pre_transaction_client = schema_editor.client
                try:
                    async with Transactions.atomic(
                        self.connection.connection_alias, lock_timeout=self.lock_timeout
                    ) as transaction_client:
                        schema_editor.client = transaction_client
                        state = await self._run_operations(migration, state, schema_editor, forwards, dry_run)
                        if not dry_run:
                            await schema_editor.check_constraints()
                            # On the transaction the DDL ran on - a crash between the two can't
                            # leave the schema changed but the record unchanged.
                            await self.record(migration, applied=forwards, connection=transaction_client)
                finally:
                    schema_editor.client = pre_transaction_client
                return state
            try:
                state = await self._run_operations(migration, state, schema_editor, forwards, dry_run)
            except Exception as error:
                raise PartiallyAppliedMigrationError(
                    MigrationRunner.get_partial_failure_message(migration, forwards, error)
                ) from error
            if not dry_run:
                await self.record(migration, applied=forwards)
        return state

    @staticmethod
    async def _run_operations(
        migration: Migration, state: State, schema_editor: BaseSchemaEditor, forwards: bool, dry_run: bool
    ) -> State:
        """Runs a migration's operations one way.

        Args:
            migration: The migration.
            state: The state before it.
            schema_editor: The schema editor it runs with.
            forwards: True to apply it, False to unapply it.
            dry_run: Run the operations without touching the database.

        Returns:
            The state after it - ``state`` itself when unapplying.
        """
        if forwards:
            return await migration.apply(state, dry_run=dry_run, schema_editor=schema_editor)
        await migration.unapply(state, dry_run=dry_run, schema_editor=schema_editor)
        return state

    @staticmethod
    def get_partial_failure_message(migration: Migration, forwards: bool, error: Exception) -> str:
        """The message of a non-atomic migration that failed partway.

        Args:
            migration: The migration.
            forwards: Whether it was being applied.
            error: The failure.

        Returns:
            The message.
        """
        if forwards:
            return (
                f"{migration} failed partway through its (non-atomic) apply - some of its operations may "
                f"already have landed on the schema. Recorded as NOT applied, so a plain retry would replay "
                f"every operation from the start, including ones that already succeeded. Inspect the schema "
                f"and fix forward manually (or mark this migration applied once it genuinely matches) before "
                f"retrying: {error}"
            )
        return (
            f"{migration} failed partway through its (non-atomic) unapply - the schema may now be a mix of "
            f"the old and new state. Recorded as still applied. Inspect the schema and fix forward manually "
            f"before retrying: {error}"
        )

    async def record(self, migration: Migration, *, applied: bool, connection: DatabaseClient | None = None) -> None:
        """Records a migration as applied or unapplied - nothing without a recorder. A squashed
        migration is recorded together with every migration it replaces.

        Args:
            migration: The migration.
            applied: True for applied, False for unapplied.
            connection: The connection to record on, the recorder's own when None.
        """
        if self.recorder is None:
            return
        if not migration.replaces:
            if applied:
                await self.recorder.record_applied(migration.app_label, migration.name, connection=connection)
            else:
                await self.recorder.record_unapplied(migration.app_label, migration.name, connection=connection)
            return
        recorded_keys = set(await self.recorder.applied_migrations(connection))
        for app_label, name in [(migration.app_label, migration.name), *migration.replaces]:
            is_recorded = MigrationKey(app_label=app_label, name=name) in recorded_keys
            if applied and not is_recorded:
                await self.recorder.record_applied(app_label, name, connection=connection)
            elif not applied and is_recorded:
                await self.recorder.record_unapplied(app_label, name, connection=connection)

    async def collect_sql(self, migration: Migration, state: State, *, backward: bool = False) -> list[str]:
        """The SQL of a migration without running it - each operation after a comment naming it,
        ``BEGIN;``/``COMMIT;`` around an atomic one.

        Args:
            migration: The migration.
            state: The state before the migration - left as it is.
            backward: The SQL unapplying it instead.

        Returns:
            The SQL statements and comments.
        """
        schema_editor = self.get_schema_editor(atomic=migration.atomic, collect_sql=True)
        if backward:
            await migration.unapply(state.clone(), schema_editor=schema_editor, collect_sql=True)
        else:
            await migration.apply(state.clone(), schema_editor=schema_editor, collect_sql=True)
        if schema_editor.atomic_migration and schema_editor.collected_sql:
            transactions = schema_editor.client.dialect.transactions
            return [
                f"{transactions.get_begin_sql()};",
                *schema_editor.collected_sql,
                f"{transactions.get_commit_sql()};",
            ]
        return schema_editor.collected_sql

    def get_effects(self, migration: Migration, state: State) -> list[OperationEffect]:
        """What each operation of a migration does to the data, known before it runs - whether it
        can be unapplied, rewrites a table, loses data.

        Args:
            migration: The migration.
            state: The state before the migration - left as it is.

        Returns:
            One effect per operation, in order.
        """
        dialect = self.connection.dialect
        operation_state = state.clone()
        effects = []
        for operation in migration.operations:
            effects.append(operation.get_effect(migration.app_label, operation_state, dialect))
            operation.state_forward(migration.app_label, operation_state)
        return effects
