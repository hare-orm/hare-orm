from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS
from hare.migrations.operations import CreateModel, RenameModel, SeparateDatabaseAndState
from hare.migrations.safety.migration_safety_context import MigrationSafetyContext
from hare.migrations.safety.operation_states import OperationStates

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
    from hare.migrations.migration import Migration
    from hare.migrations.safety.migration_risk import MigrationRisk
    from hare.migrations.state.state import State


class MigrationSafetyChecker:
    """Finds the operations of a migration that are risky on a database in use - a table locked or
    rewritten for long, running code broken by a renamed or dropped column - by the rules of the
    database's dialect (``Dialect.migration_safety_rules``). Read from a live database, a table
    counts as large from ``large_table_rows`` rows; without one, every table that exists before the
    migration may be large.

    Args:
        large_table_rows: The rows from which a table counts as large.
    """

    def __init__(self, *, large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS) -> None:
        self.large_table_rows = large_table_rows

    async def check(
        self,
        migration: Migration,
        state: State,
        *,
        dialect: Dialect,
        features: Features | None = None,
        client: DatabaseClient | None = None,
    ) -> list[MigrationRisk]:
        """Checks one migration.

        Args:
            migration: The migration.
            state: The migration state before it - left as it is.
            dialect: The dialect of the database it runs on.
            features: What the database supports - the dialect's when not given.
            client: A connection to the database, to count the rows of the tables it changes;
                without it every table that exists before the migration may be large.

        Returns:
            Its risks, in the order of its operations - exempted ones included, marked.
        """
        rules = dialect.migration_safety_rules.rules
        created_model_names = frozenset(
            operation.name.lower()
            for operation in migration.operations
            if isinstance(operation, CreateModel) and not operation.state_only
        )
        table_row_counts = (
            await self.count_table_rows(migration, state, dialect, client, created_model_names)
            if client is not None
            else None
        )
        common_context_values: dict[str, Any] = {
            "migration": migration,
            "dialect": dialect,
            "features": features or dialect.features,
            "large_table_rows": self.large_table_rows,
            "table_row_counts": table_row_counts,
            "created_model_names": created_model_names,
        }
        risks: list[MigrationRisk] = []
        contexts: list[MigrationSafetyContext] = []
        operation_states = OperationStates(migration.app_label, migration.operations, state)
        for position, operation in enumerate(migration.operations):
            context = MigrationSafetyContext(
                operation=operation, operation_states=operation_states, position=position, **common_context_values
            )
            contexts.append(context)
            risks.extend(self.check_operation(rules, context))
            if isinstance(operation, SeparateDatabaseAndState):
                database_operation_states = OperationStates(
                    migration.app_label, operation.database_operations, operation_states.get(position)
                )
                for database_position, database_operation in enumerate(operation.database_operations):
                    risks.extend(
                        self.check_operation(
                            rules,
                            MigrationSafetyContext(
                                operation=database_operation,
                                operation_states=database_operation_states,
                                position=database_position,
                                in_separate_database_and_state=True,
                                **common_context_values,
                            ),
                        )
                    )
        for rule in rules:
            risks.extend(rule.check_migration(contexts))
        return risks

    @staticmethod
    def check_operation(
        rules: tuple[MigrationSafetyRule, ...], context: MigrationSafetyContext
    ) -> list[MigrationRisk]:
        """Applies every rule to one operation.

        Args:
            rules: The dialect's rules.
            context: The operation and what is known about it.

        Returns:
            Its risks.
        """
        risks = []
        for rule in rules:
            if context.in_separate_database_and_state and not rule.checks_separated_database_operations:
                continue
            risk = rule.check_operation(context)
            if risk is not None:
                risks.append(risk)
        return risks

    async def count_table_rows(
        self,
        migration: Migration,
        state: State,
        dialect: Dialect,
        client: DatabaseClient,
        created_model_names: frozenset[str],
    ) -> dict[str, int | None]:
        """Counts the rows of each table the migration changes that exists before it, up to
        ``large_table_rows`` - a renamed model's under its new name too.

        Args:
            migration: The migration.
            state: The migration state before it.
            dialect: The database's dialect.
            client: The connection.
            created_model_names: The lowercase names of the models the migration creates.

        Returns:
            The counts by lowercase model name.
        """
        table_row_counts: dict[str, int | None] = {}
        for operation in migration.operations:
            if isinstance(operation, RenameModel):
                old_name = operation.old_name.lower()
                if old_name not in table_row_counts:
                    table_row_counts[old_name] = await self.count_model_rows(
                        migration, state, dialect, client, old_name
                    )
                table_row_counts[operation.new_name.lower()] = table_row_counts[old_name]
                continue
            for model_name in operation.get_table_model_names():
                lowercase_name = model_name.lower()
                if lowercase_name in created_model_names or lowercase_name in table_row_counts:
                    continue
                table_row_counts[lowercase_name] = await self.count_model_rows(
                    migration, state, dialect, client, lowercase_name
                )
        return table_row_counts

    async def count_model_rows(
        self, migration: Migration, state: State, dialect: Dialect, client: DatabaseClient, lowercase_name: str
    ) -> int | None:
        """Counts a model's table rows, up to ``large_table_rows``.

        Args:
            migration: The migration.
            state: The migration state before it.
            dialect: The database's dialect.
            client: The connection.
            lowercase_name: The model's lowercase name.

        Returns:
            The count; 0 for a model the state doesn't have yet.
        """
        model_name = next(
            (
                name
                for app_label, name in state.models
                if app_label == migration.app_label and name.lower() == lowercase_name
            ),
            None,
        )
        if model_name is None:
            return 0
        model = state.apps.get_model(f"{migration.app_label}.{model_name}")
        return await dialect.migration_safety_rules.count_table_rows(
            client, model._meta.db_table, model._meta.schema, self.large_table_rows
        )
