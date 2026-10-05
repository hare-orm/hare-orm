from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.literal_value import LiteralValue
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.node import TNode


class Array(Tuple):
    """An array of values - bound as one parameter, or written as an ``ARRAY[...]`` literal.

    Its elements stay as given: a plain value is bound or written by the array itself and is never a
    node of the tree, so a walk over the tree (``nodes_()``) costs nothing per element; only an
    element that is a SQL term is one.
    """

    def __init__(self, *values: Any) -> None:
        # Not Tuple.__init__: the parameterized render reads only original_value, and wrapping every
        # element up front cost 97% of building a large list.
        Term.__init__(self)
        self.original_value = list(values)
        self._values: list[LiteralValue | Tuple | ValueWrapper] | None = None
        #: The elements that are nodes, found once - None until a walk asks, with the list they were
        #: found in.
        self._node_elements: tuple[list[Any], list[Node]] | None = None

    @property
    def values(self) -> list[LiteralValue | Tuple | ValueWrapper]:
        if self._values is None:
            self._values = [self.wrap_constant(value) for value in self.original_value]
        return self._values

    @values.setter
    def values(self, new_values: list[LiteralValue | Tuple | ValueWrapper]) -> None:
        self._values = new_values

    def get_node_elements(self) -> list[Node]:
        """The elements that are nodes of the tree - a SQL term, or a list or tuple of values, which
        becomes an array or a row of its own.

        Returns:
            The elements, in order; the wrapped elements when they were read through ``values``.
        """
        if self._values is not None:
            return list(self._values)
        node_elements = self._node_elements
        if node_elements is None or node_elements[0] is not self.original_value:
            original_value = self.original_value
            element_types = set(map(type, original_value))
            if any(issubclass(element_type, (Node, list, tuple)) for element_type in element_types):
                elements: list[Node] = [
                    self.wrap_constant(value) if isinstance(value, (list, tuple)) else value
                    for value in original_value
                    if isinstance(value, (Node, list, tuple))
                ]
            else:
                elements = []
            node_elements = self._node_elements = (original_value, elements)
        return node_elements[1]

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for element in self.get_node_elements():
            yield from element.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        # A plain element is a constant - it fits either way and has no vote.
        return Term.get_combined_is_aggregate([element.is_aggregate for element in self.get_node_elements()])

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces the table of every element that is a term - a plain element has none.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the array with the tables replaced.
        """
        if self._values is not None or self.get_node_elements():
            self.values = [value.replace_table(current_table, new_table) for value in self.values]
            self._node_elements = None
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        if sql_context.parameterizer is None or not sql_context.parameterizer.should_parameterize(self.original_value):
            if sql_context.parameterizer is not None:
                sql_context.parameterizer.record_literal(self)
            sql = sql_context.dialect.literals.get_array_literal_sql(
                [term.get_sql(sql_context) for term in self.values]
            )
            return sql_context.format_alias_sql(sql, self.alias)

        parameter = sql_context.parameterizer.create_parameter(self.original_value, self)
        return sql_context.format_alias_sql(parameter.get_sql(sql_context), self.alias)
