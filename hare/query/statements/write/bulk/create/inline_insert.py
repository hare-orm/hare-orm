from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause

if TYPE_CHECKING:
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class InlineInsert:
    """Render already converted rows of an already selected insert batch.

    This renderer does not group objects, read model values or compute defaults.
    It uses the same INSERT builder as parameterized statements; SQL text is never
    searched or rewritten to replace placeholders inside literals or expressions.
    """

    @staticmethod
    def render(
        bulk_create: BulkCreateQuery[Any],
        columns: list[str],
        rows: Sequence[Sequence[Any]],
        *,
        returning: Sequence[Any] = (),
        rows_source_sql: str | None = None,
    ) -> str:
        """Render one bound batch, including its conflict and returning clauses."""
        return str(
            bulk_create._get_insert_statement().get_query(
                columns,
                rows,
                conflict=ConflictClause.get_conflict(bulk_create),
                returning=returning,
                rows_source_sql=rows_source_sql,
            )
        )
