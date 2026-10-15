from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.functions.greatest_least import GreatestLeast
from hare.sql.terms.criteria.is_not_true_criterion import IsNotTrueCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class ClickhouseConditionRenderers:
    """How ClickHouse writes the conditions ISO SQL writes with a form it lacks - ``IS NOT TRUE`` - and
    the greatest and the least of values some of which are NULL."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the condition renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(IsNotTrueCriterion, cls.render_is_not_true)
        renderers.register(GreatestLeast, cls.render_greatest_least)

    @staticmethod
    def render_is_not_true(criterion: IsNotTrueCriterion, sql_context: SqlContext) -> str:
        """``NOT ifNull(<term>, 0)`` - TRUE when the term is FALSE or NULL."""
        return f"NOT ifNull({criterion.term.get_sql(sql_context)}, 0)"

    @staticmethod
    def render_greatest_least(function: GreatestLeast, sql_context: SqlContext) -> str:
        """``GREATEST``/``LEAST`` skipping NULLs whatever the server gives for one - each value read
        as the first value there is from it on: NULL only when every value is."""
        values_sql = [function.get_arg_sql(argument, sql_context) for argument in function.args]
        present_values_sql = [
            f"coalesce({', '.join([*values_sql[position:], *values_sql[:position]])})"
            for position in range(len(values_sql))
        ]
        return f"{function.name}({', '.join(present_values_sql)})"
