from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.enums import PostgresqlArrayOperators
from hare.dialects.postgresql.functions.array.array_slice import ArraySlice
from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript
from hare.dialects.postgresql.lookups.array.array_length import ArrayLength
from hare.fields.data.containers.array_field import ArrayField
from hare.sql.functions.cast import Cast
from hare.sql.terms.containers import (
    ArrayContainedByTerm,
    ArrayContainsTerm,
    ArrayElementTerm,
    ArrayLengthTerm,
    ArrayOverlapTerm,
    ArraySliceTerm,
    ContainerLiteral,
)
from hare.sql.terms.criteria.basic_criterion import BasicCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class PostgresqlContainerRenderers:
    """How PostgreSQL writes the terms of arrays - subscripts, slices, ``cardinality``, the
    ``@>``/``<@``/``&&`` operators and an array literal cast to its column's type."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the renderers.

        Args:
            renderers: The dialect's renderers.
        """
        renderers.register(ArrayElementTerm, cls.render_element)
        renderers.register(ArraySliceTerm, cls.render_slice)
        renderers.register(ArrayLengthTerm, cls.render_length)
        renderers.register(ArrayContainsTerm, cls.render_contains)
        renderers.register(ArrayContainedByTerm, cls.render_contained_by)
        renderers.register(ArrayOverlapTerm, cls.render_overlap)
        renderers.register(ContainerLiteral, cls.render_literal)

    @staticmethod
    def render_element(term: ArrayElementTerm, sql_context: SqlContext) -> str:
        element_field = term.element_field
        subarray_field = element_field if isinstance(element_field, ArrayField) else None
        return ArraySubscript(term.args[0], term.index, subarray_field=subarray_field).get_sql(sql_context)

    @staticmethod
    def render_slice(term: ArraySliceTerm, sql_context: SqlContext) -> str:
        return ArraySlice(term.args[0], term.start, term.end).get_sql(sql_context)

    @staticmethod
    def render_length(term: ArrayLengthTerm, sql_context: SqlContext) -> str:
        return ArrayLength.get_term(term.args[0]).get_sql(sql_context)

    @staticmethod
    def render_operator(operator: PostgresqlArrayOperators, term: Any, sql_context: SqlContext) -> str:
        """``array <operator> other``.

        Args:
            operator: The array operator.
            term: The term of the two arrays.
            sql_context: The context.

        Returns:
            The SQL.
        """
        return BasicCriterion(operator, term.args[0], term.args[1]).get_sql(sql_context)

    @classmethod
    def render_contains(cls, term: ArrayContainsTerm, sql_context: SqlContext) -> str:
        return cls.render_operator(PostgresqlArrayOperators.CONTAINS, term, sql_context)

    @classmethod
    def render_contained_by(cls, term: ArrayContainedByTerm, sql_context: SqlContext) -> str:
        return cls.render_operator(PostgresqlArrayOperators.CONTAINED_BY, term, sql_context)

    @classmethod
    def render_overlap(cls, term: ArrayOverlapTerm, sql_context: SqlContext) -> str:
        return cls.render_operator(PostgresqlArrayOperators.OVERLAP, term, sql_context)

    @staticmethod
    def render_literal(term: ContainerLiteral, sql_context: SqlContext) -> str:
        # The type from the field itself - an array annotation's field (ArrayAgg's result, an item of
        # a nested array) is bound to no model.
        return Cast(term.value_wrapper, term.field.get_column_type(sql_context.dialect)).get_sql(sql_context)
