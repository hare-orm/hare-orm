from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.constants import DEFAULT_LARGE_TABLE_ROWS
from hare.migrations.safety.migration_safety_context import MigrationSafetyContext
from hare.migrations.safety.migration_table_rows import MigrationTableRows
from hare.migrations.state.prepared_operation_states import PreparedOperationStates

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
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
        client: DatabaseClient,
        read_catalog: bool = True,
    ) -> list[MigrationRisk]:
        """Checks one migration.

        Args:
            migration: The migration.
            state: The migration state before it - left as it is.
            client: The configured client used to prepare the same schema plans as execution.
            read_catalog: Count table rows using the client. When False, preparation makes
                no database calls and every existing table may be large.

        Returns:
            Its risks, in the order of its operations - exempted ones included, marked.
        """
        dialect = client.dialect
        rules = dialect.migration_safety_rules.rules
        operation_states = migration.prepare(state)
        table_rows = MigrationTableRows(
            operation_states.before, client, self.large_table_rows, read_catalog=read_catalog
        )
        common_context_values: dict[str, Any] = {
            "migration": migration,
            "dialect": dialect,
            "features": client.features,
            "schema_editor": dialect.schema_editor_class(client, collect_sql=True),
            "large_table_rows": self.large_table_rows,
        }
        contexts = await self.get_contexts(operation_states, common_context_values, table_rows)
        risks = [risk for context in contexts for risk in self.check_operation(rules, context)]
        for rule in rules:
            risks.extend(
                rule.check_migration(
                    [
                        context
                        for context in contexts
                        if not context.in_separate_database_and_state or rule.checks_separated_database_operations
                    ]
                )
            )
        return risks

    @classmethod
    async def get_contexts(
        cls,
        states: PreparedOperationStates,
        common_context_values: dict[str, Any],
        table_rows: MigrationTableRows,
        *,
        in_separate_database_and_state: bool = False,
    ) -> list[MigrationSafetyContext]:
        """Flatten executed leaves, retaining each boundary's independent row estimates.

        Whole-migration rules see operations across container boundaries together. State-only
        branches and containers themselves cannot supply physical row-count evidence.
        """
        contexts = []
        for position, prepared in enumerate(states.operations):
            if prepared.database_operations is not None:
                contexts.extend(
                    await cls.get_contexts(
                        prepared.database_operations,
                        common_context_values,
                        table_rows,
                        in_separate_database_and_state=True,
                    )
                )
                continue
            context = MigrationSafetyContext(
                operation=prepared.operation,
                operation_states=states,
                position=position,
                in_separate_database_and_state=in_separate_database_and_state,
                table_row_counts=await table_rows.get_counts(prepared, states.app_label),
                **common_context_values,
            )
            contexts.append(context)
            table_rows.advance(prepared, states.app_label)
        return contexts

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
