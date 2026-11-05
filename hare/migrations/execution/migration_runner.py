"""Runs one migration on one connection: applies or unapplies it in a transaction and records it,
renders its SQL without running it, and tells what its operations do to the data - for migration
files and for migrations an application builds in memory or keeps in its database alike."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.config.migrations_config import MigrationsConfig
from hare.core.connections.connections import Connections
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.migrations.exceptions import PartiallyAppliedMigrationError
from hare.migrations.execution.prepared_operation_execution import PreparedOperationExecution
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.operations import RunPython, SeparateDatabaseAndState
from hare.transactions.transaction_options import TransactionOptions

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
    from hare.migrations.migration import Migration
    from hare.migrations.reports.operation_effect import OperationEffect
    from hare.migrations.state.prepared_operation_states import PreparedOperationStates
    from hare.migrations.state.state import State


class MigrationRunner:
    """Applies and unapplies migrations on a connection.

    An atomic migration (``Migration.atomic``, the default) runs in one transaction with its
    record in the journal, where the database runs DDL in a transaction; a non-atomic one runs
    operation by operation and raises ``PartiallyAppliedMigrationError`` when one fails partway.
    Constraint checks are deferred while it runs and checked before the commit.

    Args:
        connection: The connection the migrations run on. Its handler is retained; for a standalone
            client without a handler, the current handler is captured once. An active transaction
            on this exact client is reused without substituting another client with the same alias.
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
        self.lock_timeout = MigrationsConfig(lock_timeout=lock_timeout).lock_timeout
        self.connection = connection
        self.connections = (
            connection.connection_handler if connection.connection_handler is not None else Connections.current()
        )
        if connection.connection_handler is None:
            connection.connection_handler = self.connections
        self.recorder = recorder

    def get_schema_editor(self, *, atomic: bool = True, collect_sql: bool = False) -> BaseSchemaEditor:
        """A schema editor of the connection's dialect.

        Args:
            atomic: Whether the migration it runs is atomic.
            collect_sql: Collect the SQL instead of running it.

        Returns:
            The schema editor.
        """
        connection = self.connection
        active = self.connections.get_frame().get(connection.connection_alias)
        # Reuse a transaction on this exact client, never another pool or schema
        # merely sharing its alias. Explicit transaction clients nest themselves.
        if isinstance(active, TransactionClient) and active.get_non_transactional_client() is connection:
            connection = active
        editor_class = connection.dialect.schema_editor_class
        return editor_class(connection, atomic=atomic, collect_sql=collect_sql)

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
            connection_token = self.connections.set(session_client.connection_alias, session_client)
            try:
                schema_editor.client = session_client
                yield schema_editor
            finally:
                connection_token.reset()

    async def apply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State:
        """Apply a migration, returning its new state and preserving the input on failure too."""
        prepared = migration.prepare(state)
        async with self.migration_schema_editor(migration) as schema_editor:
            return await self._run(migration, prepared, schema_editor, forwards=True, dry_run=dry_run)

    async def unapply(self, migration: Migration, state_before: State, *, dry_run: bool = False) -> State:
        """Reverse a migration from its explicit before-state, which is kept unchanged."""
        migration.raise_if_irreversible()
        prepared = migration.prepare(state_before)
        async with self.migration_schema_editor(migration) as schema_editor:
            return await self._run(migration, prepared, schema_editor, forwards=False, dry_run=dry_run)

    async def _run(
        self,
        migration: Migration,
        prepared: PreparedOperationStates,
        schema_editor: BaseSchemaEditor,
        *,
        forwards: bool,
        dry_run: bool = False,
    ) -> State:
        """Applies or unapplies a migration and records it.

        Args:
            migration: The migration.
            prepared: Independent before/after snapshots for every operation.
            schema_editor: The schema editor it runs with.
            forwards: True to apply it, False to unapply it.
            dry_run: Return the simulated state without executing migration operations or
                changing the journal. Connection and transaction setup may still run.

        Returns:
            The state after it.

        Raises:
            IrreversibleMigrationError: An operation can't be unapplied.
            PartiallyAppliedMigrationError: Execution failed after data or schema may have changed;
                the migration's journal record is left as it was.
        """
        # Entered on schema_editor.client as it is before the transaction below opens it - a database
        # may ignore turning its foreign key checks off inside an open transaction.
        async with schema_editor.constraint_checking_disabled():
            if schema_editor.atomic_migration:
                # Restored once the transaction commits, before constraint_checking_disabled()'s
                # own cleanup runs on it - not on the finished transaction's client.
                pre_transaction_client = schema_editor.client
                try:
                    async with pre_transaction_client._in_transaction(
                        TransactionOptions(lock_timeout=self.lock_timeout)
                    ) as transaction_client:
                        schema_editor.client = transaction_client
                        state = await self._run_operations(migration, prepared, schema_editor, forwards, dry_run)
                        if not dry_run:
                            await schema_editor.check_constraints()
                            # On the transaction the DDL ran on - a crash between the two can't
                            # leave the schema changed but the record unchanged.
                            await self.record(migration, applied=forwards, connection=transaction_client)
                finally:
                    schema_editor.client = pre_transaction_client
                return state
            if forwards and not dry_run:
                # Refused before any operation runs - none of them is undone by a transaction.
                PreparedOperationExecution.check_support(migration, prepared, schema_editor)
            run_statement_count = schema_editor.run_statement_count
            try:
                state = await self._run_operations(migration, prepared, schema_editor, forwards, dry_run)
            except Exception as error:
                if schema_editor.run_statement_count == run_statement_count and not MigrationRunner.runs_python(
                    migration
                ):
                    # Refused before its first statement ran - nothing of it landed.
                    raise
                raise PartiallyAppliedMigrationError(
                    MigrationRunner.get_partial_failure_message(migration, forwards, error)
                ) from error
            if not dry_run:
                await self.record(migration, applied=forwards)
        return state

    @staticmethod
    async def _run_operations(
        migration: Migration,
        prepared: PreparedOperationStates,
        schema_editor: BaseSchemaEditor,
        forwards: bool,
        dry_run: bool,
    ) -> State:
        """Execute the prepared tree once, returning an independent result snapshot."""
        if not dry_run:
            await PreparedOperationExecution.run(migration, prepared, schema_editor, backward=not forwards)
        return (prepared.after if forwards else prepared.before).clone()

    @staticmethod
    def runs_python(migration: Migration) -> bool:
        """Whether a migration runs Python code - which may write without a statement of the schema editor.

        Args:
            migration: The migration.

        Returns:
            True when an operation, or a database operation of a ``SeparateDatabaseAndState`` at any depth,
            is ``RunPython``.
        """
        operations = list(migration.operations)
        while operations:
            operation = operations.pop()
            if isinstance(operation, RunPython):
                return True
            if isinstance(operation, SeparateDatabaseAndState):
                operations.extend(operation.database_operations)
        return False

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
            await migration.unapply(state, schema_editor=schema_editor, collect_sql=True)
        else:
            await migration.apply(state, schema_editor=schema_editor, collect_sql=True)
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
        prepared = migration.prepare(state)
        schema_editor = self.get_schema_editor(collect_sql=True)
        return [
            item.operation.get_effect(migration.app_label, item.before, schema_editor) for item in prepared.operations
        ]
