from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.enums import PostgresqlArrayOperators
from hare.dialects.postgresql.functions.constants import ARRAY_OUTER_BRACES_PATTERN, ARRAY_OUTER_BRACES_REPLACEMENT
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
from hare.sql.terms.field import Field as ColumnTerm
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
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
    def get_array_sql(term: Any, sql_context: SqlContext) -> str:
        """Parenthesize an indexed expression unless it is a plain column.

        A subscript directly after a function call or cast is not valid PostgreSQL syntax.
        """
        array_sql = Function.get_arg_sql(term, sql_context)
        return array_sql if isinstance(term, ColumnTerm) else f"({array_sql})"

    @classmethod
    def render_element(cls, term: ArrayElementTerm, sql_context: SqlContext) -> str:
        arg_context = sql_context.copy(with_alias=False)
        array_sql = cls.get_array_sql(term.args[0], arg_context)
        subscript_sql = (
            str(term.index + 1) if term.index >= 0 else f"(array_length({array_sql}, 1) + {term.index + 1})"
        )
        element_field = term.element_field
        if not isinstance(element_field, ArrayField):
            return f"{array_sql}[{subscript_sql}]"
        # Bind the backslashes so standard_conforming_strings cannot change their interpretation.
        pattern_sql = ValueWrapper(ARRAY_OUTER_BRACES_PATTERN).get_sql(arg_context)
        replacement_sql = ValueWrapper(ARRAY_OUTER_BRACES_REPLACEMENT).get_sql(arg_context)
        row_text_sql = (
            f"regexp_replace(CAST({array_sql}[{subscript_sql}:{subscript_sql}] AS TEXT),"
            f"{pattern_sql},{replacement_sql})"
        )
        return f"CAST(NULLIF({row_text_sql},'') AS {element_field.get_column_type(sql_context.dialect)})"

    @classmethod
    def render_slice(cls, term: ArraySliceTerm, sql_context: SqlContext) -> str:
        array_sql = cls.get_array_sql(term.args[0], sql_context)
        return f"{array_sql}[{term.start + 1}:{term.end}]"

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
