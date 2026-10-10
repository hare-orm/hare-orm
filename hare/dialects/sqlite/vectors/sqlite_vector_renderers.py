from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import SQLITE_VECTOR_DISTANCE_FUNCTION_NAMES
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.vectors.terms.vector_distance_term import VectorDistanceTerm
from hare.vectors.terms.vector_literal import VectorLiteral

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteVectorRenderers:
    """How SQLite writes the vector terms - sqlite-vec's distance functions, a bound vector as it is."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the vector renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(VectorDistanceTerm, cls.render_vector_distance)
        renderers.register(VectorLiteral, cls.render_vector_literal)

    @staticmethod
    def render_vector_distance(function: VectorDistanceTerm, sql_context: SqlContext) -> str:
        """``vec_distance_l2(a,b)``, ``vec_distance_cosine(a,b)``, or hare's negative inner product
        - NULL when a vector is NULL, which sqlite-vec's functions refuse. A bound vector is never
        NULL and isn't tested."""
        # Rendered in the order the SQL text holds them - a parameter is bound by its position.
        null_test_sql = " OR ".join(
            f"{Function.get_arg_sql(argument, sql_context)} IS NULL"
            for argument in function.args
            if not isinstance(argument, (ValueWrapper, VectorLiteral))
        )
        arguments_sql = ",".join(Function.get_arg_sql(argument, sql_context) for argument in function.args)
        distance_sql = f"{SQLITE_VECTOR_DISTANCE_FUNCTION_NAMES[function.distance_type]}({arguments_sql})"
        if not null_test_sql:
            return distance_sql
        return f"CASE WHEN {null_test_sql} THEN NULL ELSE {distance_sql} END"

    @staticmethod
    def render_vector_literal(literal: VectorLiteral, sql_context: SqlContext) -> str:
        """The bound vector as it is - sqlite-vec reads the float32 vector of a parameter."""
        return Function.get_arg_sql(literal.args[0], sql_context)
