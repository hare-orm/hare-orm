from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.migration import Migration
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.prepared_operation import PreparedOperation
    from hare.migrations.state.prepared_operation_states import PreparedOperationStates
    from hare.migrations.state.state import State


class PreparedOperationExecution:
    """Execute every database branch with the same routing and transaction policy.

    A separate database/state operation is a transaction boundary around its
    database children, not another executor. Each leaf receives actual before
    and after snapshots in the direction in which it executes.
    """

    @classmethod
    async def run_in_constraint_scope(
        cls,
        migration: Migration,
        states: PreparedOperationStates,
        editor: BaseSchemaEditor,
        *,
        backward: bool = False,
        collect_sql: bool = False,
    ) -> None:
        """Give direct migration execution the editor's constraint ownership contract.

        The migration runner opens this scope around its transaction and journal instead.
        Direct apply/unapply have no runner, so must enter it before operation transactions.
        Borrowed transactions and previews retain the dialect's existing no-toggle policy.
        """
        if collect_sql or editor.collect_sql:
            previous_collect_sql = editor.collect_sql
            editor.collect_sql = True
            try:
                await cls.run(migration, states, editor, backward=backward)
            finally:
                editor.collect_sql = previous_collect_sql
            return
        async with editor.constraint_checking_disabled() as check_constraints:
            previous_client = editor.client
            previous_atomic = editor.atomic_migration
            transaction = (
                previous_client._in_transaction()
                if previous_atomic and not previous_client.is_transaction_client
                else nullcontext(previous_client)
            )
            try:
                async with transaction as client:
                    editor.client = client
                    editor.atomic_migration = client.is_transaction_client
                    await cls.run(migration, states, editor, backward=backward)
                    if check_constraints:
                        await editor.check_constraints()
            finally:
                editor.client = previous_client
                editor.atomic_migration = previous_atomic

    @classmethod
    async def run(
        cls,
        migration: Migration,
        states: PreparedOperationStates,
        editor: BaseSchemaEditor,
        *,
        backward: bool = False,
    ) -> None:
        operations = reversed(states.operations) if backward else states.operations
        for prepared in operations:
            await cls.run_operation(migration, prepared, editor, backward=backward)

    @classmethod
    async def run_operation(
        cls,
        migration: Migration,
        prepared: PreparedOperation,
        editor: BaseSchemaEditor,
        *,
        backward: bool,
    ) -> None:
        from hare.query.execution.query_execution import QueryExecution

        before = prepared.before
        with QueryExecution(before.environment.context):
            operation = prepared.operation
            database_operations = prepared.database_operations
            if database_operations is not None:
                if editor.collect_sql:
                    editor.collected_sql.extend(("--", f"-- {operation.describe()}", "--"))
                    if not database_operations.operations:
                        editor.collected_sql.append("-- (no-op)")
                await cls.run_atomic(
                    migration,
                    operation,
                    editor,
                    lambda: cls.run(
                        migration,
                        database_operations,
                        editor,
                        backward=backward,
                    ),
                )
                return
            states = (prepared.after, before) if backward else (before, prepared.after)
            if not cls.should_run_operation(migration, operation, editor, states):
                return
            run = operation.database_backward if backward else operation.database_forward

            async def execute() -> None:
                if not editor.collect_sql:
                    await editor.prepare_schema()
                await cls.run_atomic(migration, operation, editor, lambda: run(migration.app_label, *states, editor))

            if editor.collect_sql:
                await cls.collect_operation_sql(operation, editor, execute)
            else:
                await execute()

    @classmethod
    def check_support(
        cls,
        migration: Migration,
        states: PreparedOperationStates,
        editor: BaseSchemaEditor,
    ) -> None:
        """Check every database leaf against the same states execution receives."""
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(states.before.environment.context):
            for prepared in states.operations:
                if prepared.database_operations is not None:
                    cls.check_support(migration, prepared.database_operations, editor)
                    continue
                operation = prepared.operation
                boundaries = (prepared.before, prepared.after)
                if not cls.should_run_operation(migration, operation, editor, boundaries):
                    continue
                operation.raise_if_unsupported(migration.app_label, *boundaries, editor)

    @staticmethod
    def should_run_operation(
        migration: Migration,
        operation: Operation,
        editor: BaseSchemaEditor,
        states: tuple[State, State],
    ) -> bool:
        """Select applicable shared or tenant leaves for execution and validation."""
        if operation.touches_swapped_model(migration.app_label, *states):
            return False
        client = editor.client
        if client.tenant_schema_template is None or editor.collect_sql:
            return True
        return operation.runs_in_tenant_schema(migration.app_label, *states) is (client.tenant_schema is not None)

    @staticmethod
    async def collect_operation_sql(
        operation: Operation,
        editor: BaseSchemaEditor,
        execute: Callable[[], Awaitable[None]],
    ) -> None:
        """Render one leaf, preserving SQL siblings beside a Python-only operation."""
        editor.collected_sql.extend(("--", f"-- {operation.describe()}", "--"))
        if not operation.reduces_to_sql:
            editor.collected_sql.append("-- THIS OPERATION CANNOT BE REPRESENTED AS SQL")
            return
        before = len(editor.collected_sql)
        await execute()
        if len(editor.collected_sql) == before:
            editor.collected_sql.append("-- (no-op)")

    @staticmethod
    async def run_atomic(
        migration: Migration,
        operation: Operation,
        editor: BaseSchemaEditor,
        execute: Callable[[], Awaitable[None]],
    ) -> None:
        """Apply operation atomicity and constraint checks at every nesting depth."""
        if editor.collect_sql or editor.atomic_migration:
            await execute()
            return
        atomic = operation.atomic or (migration.atomic and operation.atomic is not False)
        if not (atomic and editor.client.features.supports_transactions):
            await execute()
            await editor.check_constraints()
            return
        previous_client = editor.client
        previous_atomic = editor.atomic_migration
        try:
            async with previous_client._in_transaction() as client:
                editor.client = client
                editor.atomic_migration = True
                await execute()
                await editor.check_constraints()
        finally:
            editor.client = previous_client
            editor.atomic_migration = previous_atomic
