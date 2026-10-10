from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.data.containers.container_field import ContainerField
from hare.sql.terms.containers import (
    ArrayContainedByTerm,
    ArrayContainsTerm,
    ArrayElementTerm,
    ArrayLengthTerm,
    ArrayOverlapTerm,
    ArraySliceTerm,
    ContainerLiteral,
    MapContainsKeyTerm,
    MapKeysTerm,
    MapValuesTerm,
    MapValueTerm,
    TupleElementTerm,
)
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class ClickhouseContainerRenderers:
    """How ClickHouse writes the terms of containers - ``arrayElement``, ``arraySlice``, ``length``,
    ``hasAll``/``hasAny``, ``m[key]``, ``mapContains``, ``tupleElement`` and a literal cast to its
    column's type."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the renderers.

        Args:
            renderers: The dialect's renderers.
        """
        renderers.register(ArrayElementTerm, cls.render_element)
        renderers.register(ArraySliceTerm, cls.render_slice)
        renderers.register(ArrayLengthTerm, cls.render_function("length"))
        renderers.register(ArrayContainsTerm, cls.render_function("hasAll"))
        renderers.register(ArrayContainedByTerm, cls.render_contained_by)
        renderers.register(ArrayOverlapTerm, cls.render_function("hasAny"))
        renderers.register(MapContainsKeyTerm, cls.render_function("mapContains"))
        renderers.register(MapKeysTerm, cls.render_function("mapKeys"))
        renderers.register(MapValuesTerm, cls.render_function("mapValues"))
        renderers.register(MapValueTerm, cls.render_map_value)
        renderers.register(TupleElementTerm, cls.render_tuple_element)
        renderers.register(ContainerLiteral, cls.render_literal)

    @staticmethod
    def get_argument_sqls(term: Function, sql_context: SqlContext) -> list[str]:
        """The SQL of a term's arguments.

        Args:
            term: The term.
            sql_context: The context.

        Returns:
            One SQL text per argument.
        """
        return [Function.get_arg_sql(argument, sql_context) for argument in term.args]

    @classmethod
    def render_function(cls, name: str) -> Any:
        """A renderer writing a term as the ClickHouse function ``name`` of its arguments.

        Args:
            name: The function.

        Returns:
            The renderer.
        """

        def render(term: Function, sql_context: SqlContext) -> str:
            return f"{name}({','.join(cls.get_argument_sqls(term, sql_context))})"

        return render

    @classmethod
    def render_contained_by(cls, term: ArrayContainedByTerm, sql_context: SqlContext) -> str:
        array_sql, other_sql = cls.get_argument_sqls(term, sql_context)
        return f"hasAll({other_sql},{array_sql})"

    @classmethod
    def render_element(cls, term: ArrayElementTerm, sql_context: SqlContext) -> str:
        (array_sql,) = cls.get_argument_sqls(term, sql_context)
        position = term.index + 1 if term.index >= 0 else term.index
        element_sql = f"arrayElement({array_sql},{position})"
        if isinstance(term.element_field, ContainerField):
            # A container is never NULL - past either end it is the empty one.
            return element_sql
        # arrayElement() is the type's default past either end - NULL there, as on the other databases.
        return f"if(length({array_sql})>={abs(position)},{element_sql},NULL)"

    @classmethod
    def render_slice(cls, term: ArraySliceTerm, sql_context: SqlContext) -> str:
        (array_sql,) = cls.get_argument_sqls(term, sql_context)
        return f"arraySlice({array_sql},{term.start + 1},{max(term.end - term.start, 0)})"

    @classmethod
    def render_map_value(cls, term: MapValueTerm, sql_context: SqlContext) -> str:
        map_sql, key_sql = cls.get_argument_sqls(term, sql_context)
        return f"{map_sql}[{key_sql}]"

    @classmethod
    def render_tuple_element(cls, term: TupleElementTerm, sql_context: SqlContext) -> str:
        (tuple_sql,) = cls.get_argument_sqls(term, sql_context)
        return f"tupleElement({tuple_sql},{term.index + 1})"

    @staticmethod
    def render_literal(term: ContainerLiteral, sql_context: SqlContext) -> str:
        value_sql = term.value_wrapper.get_sql(sql_context)
        return f"CAST({value_sql} AS {term.field.get_column_type(sql_context.dialect)})"
