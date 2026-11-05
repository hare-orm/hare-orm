from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from hare.fields.field import Field
from hare.migrations.constants import RUNTIME_MIGRATION_MODULE_PREFIX
from hare.migrations.exceptions import IrreversibleMigrationError, MigrationLoadError
from hare.migrations.execution.prepared_operation_execution import PreparedOperationExecution
from hare.migrations.operations import Operation
from hare.migrations.state.prepared_operation_states import PreparedOperationStates
from hare.migrations.state.state import State

if TYPE_CHECKING:
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

    @staticmethod
    def from_source(source: str, *, name: str, app_label: str) -> Migration:
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
            # A migration replays a schema it was written for - its fields are built as the migration
            # loader builds those of a migration file (an omitted db_index is the False it was written as).
            with Field.replaying_migration_scope():
                exec(code, namespace)  # nosec B102 - the source of a migration the application itself keeps
        except Exception as error:
            raise MigrationLoadError(f"Migration {app_label}.{name} can't be read from its source: {error}") from error
        migration_class = namespace.get("Migration")
        if not isinstance(migration_class, type) or not issubclass(migration_class, Migration):
            raise MigrationLoadError(f"The source of migration {app_label}.{name} declares no Migration class")
        return migration_class(name, app_label)

    def prepare(self, state_before: State) -> PreparedOperationStates:
        """Prepare both sides of every operation without changing the input state."""
        prepared = PreparedOperationStates(self.app_label, self.operations, state_before)
        prepared.after.validate_relations_initialized()
        return prepared

    async def apply(
        self,
        state: State,
        *,
        dry_run: bool = False,
        schema_editor: BaseSchemaEditor | None = None,
        collect_sql: bool = False,
    ) -> State:
        """Apply the prepared operations and return a new state.

        The supplied state and operation declarations remain unchanged, including
        after an error. A dry run prepares state only; SQL collection uses the
        same operation tree and execution policies as a database run.
        """
        prepared = self.prepare(state)
        if schema_editor is not None and (collect_sql or not dry_run):
            await PreparedOperationExecution.run(self, prepared, schema_editor, collect_sql=collect_sql)
        return prepared.after.clone()

    def raise_if_irreversible(self) -> None:
        """Reject a reverse run before executing any database operation."""
        for operation in self.operations:
            if not operation.reversible:
                raise IrreversibleMigrationError(f"Operation {operation} in {self} is not reversible")

    async def unapply(
        self,
        state_before: State,
        *,
        dry_run: bool = False,
        schema_editor: BaseSchemaEditor | None = None,
        collect_sql: bool = False,
    ) -> State:
        """Reverse the migration using its explicit state before application.

        Both database states come from the same forward preparation as apply.
        The supplied state remains unchanged; the returned state is an
        independent copy of it.
        """
        self.raise_if_irreversible()
        prepared = self.prepare(state_before)
        if schema_editor is not None and (collect_sql or not dry_run):
            await PreparedOperationExecution.run(
                self,
                prepared,
                schema_editor,
                backward=True,
                collect_sql=collect_sql,
            )
        return prepared.before.clone()

    def raise_if_unsupported(self, state: State, schema_editor: BaseSchemaEditor) -> None:
        """Check the prepared database tree before executing any operation."""
        PreparedOperationExecution.check_support(self, self.prepare(state), schema_editor)
