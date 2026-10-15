from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.vectors.constants import POSTGRESQL_VECTOR_DISTANCE_OPERATORS
from hare.sql.functions.cast import Cast
from hare.sql.terms.functions.function import Function
from hare.vectors.terms.vector_distance_term import VectorDistanceTerm
from hare.vectors.terms.vector_literal import VectorLiteral

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class PostgresqlVectorRenderers:
    """How PostgreSQL writes the vector terms - pgvector's distance operators, a bound vector cast
    to ``vector``."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the vector renderers on PostgreSQL's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(VectorDistanceTerm, cls.render_vector_distance)
        renderers.register(VectorLiteral, cls.render_vector_literal)

    @staticmethod
    def render_vector_distance(function: VectorDistanceTerm, sql_context: SqlContext) -> str:
        """``(a <-> b)``, ``(a <=> b)`` or ``(a <#> b)``."""
        first, second = function.args
        operator = POSTGRESQL_VECTOR_DISTANCE_OPERATORS[function.distance_type]
        return f"({Function.get_arg_sql(first, sql_context)}{operator}{Function.get_arg_sql(second, sql_context)})"

    @staticmethod
    def render_vector_literal(literal: VectorLiteral, sql_context: SqlContext) -> str:
        """The bound vector cast to ``vector`` - a bare parameter would be typed as text."""
        return Cast(literal.args[0], "vector").get_sql(sql_context)
