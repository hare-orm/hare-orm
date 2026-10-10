from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from hare.dialects.base.clauses.enums import MergeAction, RowLockStrength
from hare.dialects.base.clauses.limit_returning_conflict_query_clauses import LimitReturningConflictQueryClauses
from hare.dialects.postgresql.clauses.constants import (
    POSTGRESQL_COPY_STATEMENT_TEMPLATE,
    POSTGRESQL_DEFAULT_EXPLAIN_FORMAT,
    POSTGRESQL_DEFAULT_EXPLAIN_OPTIONS,
    POSTGRESQL_EXPLAIN_FORMATS,
    POSTGRESQL_MERGE_MATCH_SQL,
    POSTGRESQL_OLD_ROW_VALUE_TEMPLATE,
    POSTGRESQL_ROW_LOCK_SQL,
)
from hare.exceptions import UnSupportedError
from hare.sql.builder.tables.table import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.clauses.merge_when_sql import MergeWhenSql
    from hare.dialects.base.features import Features
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.sql_context import SqlContext


class PostgresqlQueryClauses(LimitReturningConflictQueryClauses):
    """PostgreSQL's clauses - ``DISTINCT ON``, row locks, ``EXPLAIN (...)`` and the inserted flag of
    an upsert's returned row."""

    def get_bulk_load_statement_sql(self, table: str, columns: Sequence[str]) -> str:
        return POSTGRESQL_COPY_STATEMENT_TEMPLATE.format(table=table, columns=", ".join(columns))

    def get_distinct_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        distinct_context = sql_context.copy(with_alias=True)
        if builder._distinct_on:
            return "DISTINCT ON({distinct_on}) ".format(
                distinct_on=",".join(term.get_sql(distinct_context) for term in builder._distinct_on)
            )
        return "DISTINCT " if builder._distinct else ""

    def get_row_lock_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        if not builder._for_update:
            return ""
        lock_sql = POSTGRESQL_ROW_LOCK_SQL[RowLockStrength(builder._for_update_strength or RowLockStrength.UPDATE)]
        if builder._for_update_of:
            lock_sql += f" OF {', '.join(Table(item).get_sql(sql_context) for item in sorted(builder._for_update_of))}"
        if builder._for_update_nowait:
            lock_sql += " NOWAIT"
        elif builder._for_update_skip_locked:
            lock_sql += " SKIP LOCKED"
        return lock_sql

    def get_merge_when_sql(self, when: MergeWhenSql) -> str:
        """A ``WHEN`` branch - ``NOT MATCHED BY SOURCE`` and ``DO NOTHING`` too."""
        action_sql = "DO NOTHING" if when.action is MergeAction.DO_NOTHING else self.get_merge_action_sql(when)
        return f" WHEN {POSTGRESQL_MERGE_MATCH_SQL[when.match]}{self.get_merge_condition_sql(when)} THEN {action_sql}"

    def get_merge_returning_sql(self, returned: Sequence[ReturnedValue], action_alias_sql: str) -> str:
        """``RETURNING merge_action(), ...`` - PostgreSQL 17+."""
        if not returned:
            return ""
        return f" RETURNING merge_action() AS {action_alias_sql}," + ",".join(
            self.get_returned_value_sql(value) for value in returned
        )

    def get_old_row_value_sql(self, column_sql: str) -> str:
        """``old.column`` - PostgreSQL 18+."""
        return POSTGRESQL_OLD_ROW_VALUE_TEMPLATE.format(column=column_sql)

    def get_upsert_inserted_flag_sql(self) -> str | None:
        # A row ON CONFLICT DO UPDATE wrote carries the updating transaction's id in xmax.
        return "(xmax = 0)"

    def get_explain_sql(
        self, sql: str, output_format: str | None, options: Mapping[str, bool], features: Features
    ) -> str:
        """``EXPLAIN (options, FORMAT ...)`` - JSON and ``VERBOSE`` unless given."""
        output_format = (output_format or POSTGRESQL_DEFAULT_EXPLAIN_FORMAT).upper()
        if output_format not in POSTGRESQL_EXPLAIN_FORMATS:
            raise UnSupportedError(f"Unsupported explain format: {output_format}")
        required_options = sorted(
            option.upper() for option, required in (options or POSTGRESQL_DEFAULT_EXPLAIN_OPTIONS).items() if required
        )
        if unsupported_options := set(required_options) - features.explain_options:
            raise UnSupportedError(
                f"Unsupported options: {unsupported_options} - not taken by the PostgreSQL server of this connection"
            )
        return f"EXPLAIN ({', '.join([*required_options, f'FORMAT {output_format}'])}) {sql}"
