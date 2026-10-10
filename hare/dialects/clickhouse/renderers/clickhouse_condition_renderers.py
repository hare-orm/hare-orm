from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.criteria.is_not_true_criterion import IsNotTrueCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class ClickhouseConditionRenderers:
    """How ClickHouse writes the conditions ISO SQL writes with a form it lacks - ``IS NOT TRUE``."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the condition renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(IsNotTrueCriterion, cls.render_is_not_true)

    @staticmethod
    def render_is_not_true(criterion: IsNotTrueCriterion, sql_context: SqlContext) -> str:
        """``NOT ifNull(<term>, 0)`` - TRUE when the term is FALSE or NULL."""
        return f"NOT ifNull({criterion.term.get_sql(sql_context)}, 0)"
