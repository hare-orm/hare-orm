from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from hare.sql.terms.base.term import Term

TNode = TypeVar("TNode", bound="Node")


class Node:
    is_aggregate: bool | None = None
    #: A window function call - computed after grouping, so an aggregate inside it never groups
    #: the query that selects it.
    is_analytic: bool = False
    #: A subquery term (scalar subquery, EXISTS) - its own SQL is a separate query scope.
    is_subquery: bool = False
    #: The enclosing query's columns a subquery term reads through OuterRef().
    outer_reference_terms: tuple[Term, ...] = ()

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]

    def find_(self, type: type[TNode]) -> list[TNode]:
        return [  # type:ignore[var-annotated]
            node for node in self.nodes_() if isinstance(node, type)
        ]

    def _is_group_scope_boundary(self) -> bool:
        """Whether this node's inner terms are outside the enclosing query's grouping scope.

        Returns:
            True for an aggregate call, a window function call or a subquery.
        """
        # Imported here: the modules import each other.
        from hare.sql.terms.functions.aggregate_function import AggregateFunction

        return self.is_subquery or self.is_analytic or isinstance(self, AggregateFunction)

    @property
    def contains_aggregate(self) -> bool:
        """Whether this node is, or anywhere contains, an aggregate of the query it belongs to.

        Unlike `is_aggregate`'s vote, one aggregate anywhere is enough (``COUNT(x)+"id"``). An
        aggregate inside a window function or a subquery doesn't count; a subquery reading an outer
        aggregate annotation does.

        Returns:
            True when the query needs grouping for this node and it belongs in HAVING, not WHERE.
        """
        # Imported here: the modules import each other.
        from hare.sql.terms.functions.aggregate_function import AggregateFunction

        boundary_node_ids: set[int] = set()
        nodes: Iterator[Node] = self.nodes_()
        for node in nodes:
            if id(node) in boundary_node_ids:
                continue
            if node.is_subquery:
                if node.is_aggregate:
                    return True
            elif node.is_analytic:
                pass
            elif isinstance(node, AggregateFunction):
                return True
            else:
                continue
            boundary_node_ids.update(map(id, node.nodes_()))
        return False

    def get_group_by_column_terms(self) -> list[Term]:
        """The columns this node reads outside any aggregate, window function or subquery, plus the
        outer columns each subquery reads through OuterRef().

        Returns:
            The column terms, in the order they appear.
        """
        # Imported here: the modules import each other.
        from hare.sql.terms.field import Field

        column_terms: list[Term] = []
        boundary_node_ids: set[int] = set()
        nodes: Iterator[Node] = self.nodes_()
        for node in nodes:
            if id(node) in boundary_node_ids:
                continue
            if node._is_group_scope_boundary():
                if node.is_subquery:
                    column_terms.extend(node.outer_reference_terms)
                boundary_node_ids.update(map(id, node.nodes_()))
            elif isinstance(node, Field) and node.table is not None:
                column_terms.append(node)
        return column_terms

    def get_group_by_terms(self) -> list[Term]:
        """The terms an implicit GROUP BY needs for this node to be valid next to an aggregate.

        A constant needs nothing, a plain expression - a correlated subquery included - is grouped
        as a whole, and an expression mixing in an aggregate is grouped by the columns it reads
        outside it.

        Returns:
            The terms to group by.
        """
        if self.is_analytic or self.is_aggregate is None:
            return []
        if self.contains_aggregate:
            return self.get_group_by_column_terms()
        return [cast("Term", self)]
