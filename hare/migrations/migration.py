from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from hare.migrations.constants import RUNTIME_MIGRATION_MODULE_PREFIX
from hare.migrations.exceptions import IrreversibleMigrationError, MigrationLoadError
from hare.migrations.operations import Operation
from hare.migrations.state.state import State
from hare.transactions.transactions import Transactions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.safety.enums import MigrationRiskCode


class Migration:
    """A migration: operations on one app's models, applied in order.

    A migration file declares a subclass with class-level ``operations``/``dependencies``; a
    migration built in memory - for tables whose schema an application changes while it runs -
    passes them to the constructor instead, or is read from its source with ``from_source()``.

    Args:
        name: The migration's name.
        app_label: The app whose models it changes.
        operations: The operations, in place of the class's own.
        dependencies: The ``(app_label, migration_name)`` pairs it depends on, in place of the
            class's own.
    """

    operations: list[Operation] = []
    dependencies: list[tuple[str, str]] = []
    run_before: list[tuple[str, str]] = []
    replaces: list[tuple[str, str]] = []
    initial: bool | None = None
    atomic: bool = True
    #: The ``MigrationRiskCode``s the migration safety check accepts in this migration - each one
    #: checked by hand; ``checkmigrations`` still lists them, without failing.
    safety_exemptions: list[MigrationRiskCode] = []

    def __init__(
        self,
        name: str,
        app_label: str,
        *,
        operations: Iterable[Operation] | None = None,
        dependencies: Iterable[tuple[str, str]] | None = None,
    ):
        self.name = name
        self.app_label = app_label
        self.operations = list(self.__class__.operations if operations is None else operations)
        self.dependencies = list(self.__class__.dependencies if dependencies is None else dependencies)
        self.run_before = list(self.__class__.run_before)
        self.replaces = list(self.__class__.replaces)
        self.safety_exemptions = list(self.__class__.safety_exemptions)

    def __str__(self) -> str:
        return f"{self.app_label}.{self.name}"

    @classmethod
    def from_source(cls, source: str, *, name: str, app_label: str) -> Migration:
        """Reads a migration from its source - the text ``MigrationWriter.as_string()`` renders -
        without a file, e.g. a migration an application keeps in its database. The source is
        executed as Python, so it must come from a trusted place.

        Args:
            source: The migration's source, declaring a ``Migration`` class.
            name: The migration's name.
            app_label: The app whose models it changes.

        Returns:
            The migration.

        Raises:
            MigrationLoadError: The source doesn't run, or declares no ``Migration`` subclass.
        """
        namespace: dict[str, object] = {"__name__": f"{RUNTIME_MIGRATION_MODULE_PREFIX}{app_label}.{name}"}
        try:
            code = compile(source, f"<migration {app_label}.{name}>", "exec")
            exec(code, namespace)  # nosec B102 - the source of a migration the application itself keeps
        except Exception as error:
            raise MigrationLoadError(f"Migration {app_label}.{name} can't be read from its source: {error}") from error
        migration_class = namespace.get("Migration")
        if not isinstance(migration_class, type) or not issubclass(migration_class, Migration):
            raise MigrationLoadError(f"The source of migration {app_label}.{name} declares no Migration class")
        return migration_class(name, app_label)

    async def apply(
        self,
        state: State,
        *,
        dry_run: bool = False,
        schema_editor: BaseSchemaEditor | None = None,
        collect_sql: bool = False,
    ) -> State:
        supports_transactions = schema_editor is not None and schema_editor.client.features.supports_transactions
        need_old_state = (collect_sql and schema_editor) or (not dry_run and schema_editor is not None)
        for operation in self.operations:
            old_state = state.clone() if need_old_state and operation.reads_old_state else None
            operation.state_forward(self.app_label, state)
            if collect_sql and schema_editor:
                await self._collect_operation_sql(
                    operation,
                    schema_editor,
                    lambda: self._run_database_forward(
                        operation,
                        old_state,
                        state,
                        schema_editor,
                        supports_transactions,
                    ),
                )
                continue
            if dry_run or not schema_editor:
                continue
            await self._run_database_forward(
                operation,
                old_state,
                state,
                schema_editor,
                supports_transactions,
            )
        state.validate_relations_initialized()
        return state

    async def unapply(
        self,
        state: State,
        *,
        dry_run: bool = False,
        schema_editor: BaseSchemaEditor | None = None,
        collect_sql: bool = False,
    ) -> State:
        supports_transactions = schema_editor is not None and schema_editor.client.features.supports_transactions
        need_old_state = (collect_sql and schema_editor) or (not dry_run and schema_editor is not None)
        to_run: list[tuple[Operation, State, State]] = []
        new_state = state
        if not need_old_state and self.operations:
            # Single working copy so state_forward doesn't mutate the original
            new_state = state.clone()
        for operation in self.operations:
            if not getattr(operation, "reversible", True):
                raise IrreversibleMigrationError(f"Operation {operation} in {self} is not reversible")
            if need_old_state:
                new_state = new_state.clone()
                old_state = new_state.clone()
                operation.state_forward(self.app_label, new_state)
                to_run.insert(0, (operation, old_state, new_state))
            else:
                operation.state_forward(self.app_label, new_state)

        for operation, to_state, from_state in to_run:
            if collect_sql and schema_editor:
                await self._collect_operation_sql(
                    operation,
                    schema_editor,
                    lambda: self._run_database_backward(
                        operation, from_state, to_state, schema_editor, supports_transactions
                    ),
                )
                continue
            if dry_run or not schema_editor:
                continue
            await self._run_database_backward(operation, from_state, to_state, schema_editor, supports_transactions)
        return state

    @staticmethod
    async def _collect_operation_sql(
        operation: Operation, schema_editor: BaseSchemaEditor, run_operation: Callable[[], Awaitable[None]]
    ) -> None:
        """Collects the SQL of one operation for sqlmigrate: a comment naming it, then the SQL
        ``run_operation`` sends through the collecting schema editor, ``-- (no-op)`` when it sends
        none - or a note that the operation has no SQL form.

        Args:
            operation: The operation.
            schema_editor: The collecting schema editor.
            run_operation: Runs the operation forwards or backwards.
        """
        collected_sql = schema_editor.collected_sql
        collected_sql.extend(("--", f"-- {operation.describe()}", "--"))
        if not operation.reduces_to_sql:
            collected_sql.append("-- THIS OPERATION CANNOT BE REPRESENTED AS SQL")
            return
        before = len(collected_sql)
        await run_operation()
        if len(collected_sql) == before:
            collected_sql.append("-- (no-op)")

    async def _run_database_forward(
        self,
        operation: Operation,
        old_state: State | None,
        new_state: State,
        schema_editor: BaseSchemaEditor,
        supports_transactions: bool,
    ) -> None:
        # No state before an operation that doesn't read it (Operation.reads_old_state) - one adding
        # a model, a field or a schema object, whose swapped-ness the new state tells alone.
        states = (new_state,) if old_state is None else (old_state, new_state)
        if operation.touches_swapped_model(self.app_label, *states):
            return
        if not self._runs_on_client(operation, schema_editor, states):
            return
        await self._run_database_operation(
            operation,
            lambda: operation.database_forward(self.app_label, old_state, new_state, schema_editor),  # type: ignore[arg-type]
            schema_editor,
            supports_transactions,
        )

    async def _run_database_backward(
        self,
        operation: Operation,
        old_state: State,
        new_state: State,
        schema_editor: BaseSchemaEditor,
        supports_transactions: bool,
    ) -> None:
        if operation.touches_swapped_model(self.app_label, old_state, new_state):
            return
        if not self._runs_on_client(operation, schema_editor, (old_state, new_state)):
            return
        await self._run_database_operation(
            operation,
            lambda: operation.database_backward(self.app_label, old_state, new_state, schema_editor),
            schema_editor,
            supports_transactions,
        )

    def _runs_on_client(
        self, operation: Operation, schema_editor: BaseSchemaEditor, states: tuple[State, ...]
    ) -> bool:
        """Whether an operation runs on the schema editor's client - on a connection with
        ``tenant_schema_template`` the connection's own client runs the operations of the shared
        schema and a tenant schema's client those of the tenants' schemas; collected SQL has both.

        Args:
            operation: The operation.
            schema_editor: The migration's schema editor.
            states: The states before and after the operation.
        """
        client = schema_editor.client
        if client.tenant_schema_template is None or schema_editor.collect_sql:
            return True
        return operation.runs_in_tenant_schema(self.app_label, *states) is (client.tenant_schema is not None)

    async def _run_database_operation(
        self,
        operation: Operation,
        run_operation: Callable[[], Awaitable[None]],
        schema_editor: BaseSchemaEditor,
        supports_transactions: bool,
    ) -> None:
        """Runs one operation's database step, in its own transaction when the operation is
        atomic but the migration as a whole is not.

        Args:
            operation: The operation being run.
            run_operation: Performs the operation's database_forward()/database_backward().
            schema_editor: The migration's schema editor.
            supports_transactions: Whether the connection supports transactions at all.
        """
        if schema_editor.collect_sql or schema_editor.atomic_migration:
            await run_operation()
            return
        atomic_operation = operation.atomic or (self.atomic and operation.atomic is not False)
        if not (atomic_operation and supports_transactions):
            await run_operation()
            await schema_editor.check_constraints()
            return
        pre_transaction_client = schema_editor.client
        try:
            async with Transactions.atomic(pre_transaction_client.connection_alias) as transaction_client:
                schema_editor.client = transaction_client
                await run_operation()
                await schema_editor.check_constraints()
        finally:
            schema_editor.client = pre_transaction_client
