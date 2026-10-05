from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS, MAX_LARGE_TABLE_ROWS
from hare.exceptions import ConfigurationError
from hare.migrations.operations import CreateModel, RenameModel, SeparateDatabaseAndState
from hare.migrations.safety.enums import MigrationRiskCode
from hare.migrations.safety.migration_safety_context import MigrationSafetyContext

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.migration_safety.migration_safety_rule import MigrationSafetyRule
    from hare.migrations.migration import Migration
    from hare.migrations.operations import Operation
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

    Raises:
        ConfigurationError: ``large_table_rows`` isn't a whole number from 0 to 10**12.
    """

    def __init__(self, *, large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS) -> None:
        if isinstance(large_table_rows, bool) or not isinstance(large_table_rows, int):
            raise ConfigurationError(f"large_table_rows must be a whole number of rows, got {large_table_rows!r}")
        if not 0 <= large_table_rows <= MAX_LARGE_TABLE_ROWS:
            raise ConfigurationError(
                f"large_table_rows must be between 0 and {MAX_LARGE_TABLE_ROWS}, got {large_table_rows!r}"
            )
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

        Raises:
            ConfigurationError: The migration's ``safety_exemptions`` holds something other than a
                ``MigrationRiskCode``.
        """
        self.raise_for_unknown_exemptions(migration)
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
        operation_state = state.clone()
        for operation in migration.operations:
            state_after = self.get_state_after(migration.app_label, operation, operation_state)
            context = MigrationSafetyContext(
                operation=operation, state_before=operation_state, state_after=state_after, **common_context_values
            )
            contexts.append(context)
            risks.extend(self.check_operation(rules, context))
            if isinstance(operation, SeparateDatabaseAndState):
                database_state = operation_state
                for database_operation in operation.database_operations:
                    database_state_after = self.get_state_after(
                        migration.app_label, database_operation, database_state
                    )
                    risks.extend(
                        self.check_operation(
                            rules,
                            MigrationSafetyContext(
                                operation=database_operation,
                                state_before=database_state,
                                state_after=database_state_after,
                                in_separate_database_and_state=True,
                                **common_context_values,
                            ),
                        )
                    )
                    database_state = database_state_after
            operation_state = state_after
        for rule in rules:
            risks.extend(rule.check_migration(contexts))
        return risks

    @staticmethod
    def raise_for_unknown_exemptions(migration: Migration) -> None:
        """Refuses a ``safety_exemptions`` entry that isn't a ``MigrationRiskCode``.

        Args:
            migration: The migration.

        Raises:
            ConfigurationError: For the first entry that isn't one.
        """
        known_codes = set(MigrationRiskCode)
        for exemption in migration.safety_exemptions:
            if exemption not in known_codes:
                raise ConfigurationError(
                    f"Migration {migration}: safety_exemptions takes MigrationRiskCode members, got {exemption!r}"
                )

    @staticmethod
    def get_state_after(app_label: str, operation: Operation, state: State) -> State:
        """The migration state after an operation.

        Args:
            app_label: The migration's app.
            operation: The operation.
            state: The state before it - left as it is.

        Returns:
            A new state.
        """
        state_after = state.clone()
        operation.state_forward(app_label, state_after)
        return state_after

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
