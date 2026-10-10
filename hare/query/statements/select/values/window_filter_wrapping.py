from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.query.statements.select.values.constants import WINDOW_FILTER_OPERAND_CRITERION_TYPES
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.field import Field
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext


class WindowFilterWrapping:
    """A filter on a window function, which WHERE can't read: the query becomes a derived table and the
    filter is rewritten over its columns."""

    @staticmethod
    def get_aliasless_sql(term: Term, aliasless_context: SqlContext) -> str:
        """A term's SQL without its alias, to recognize the same term selected twice.

        Args:
            term: The term.
            aliasless_context: The query's namespaced SQL context, rendering no alias.

        Returns:
            The SQL text.
        """
        aliasless_term = term
        if term.alias is not None:
            aliasless_term = copy(term)
            aliasless_term.alias = None
        return aliasless_term.get_sql(aliasless_context)

    @staticmethod
    def get_derived_table_criterion(term: Term, get_column: Callable[[Term], Field]) -> Term:
        """Rewrites a criterion of the inner query into one over its derived table's columns -
        every operand reading a row becomes the column selecting it.

        Args:
            term: The criterion, or an operand of it.
            get_column: Returns the derived table's column selecting an inner query term.

        Returns:
            The rewritten term.
        """
        if (
            isinstance(term, Criterion)
            and not isinstance(term, WINDOW_FILTER_OPERAND_CRITERION_TYPES)
            and not term.is_subquery
        ):
            rewritten_criterion = copy(term)
            for attribute_name, attribute_value in vars(term).items():
                if isinstance(attribute_value, Term):
                    setattr(
                        rewritten_criterion,
                        attribute_name,
                        WindowFilterWrapping.get_derived_table_criterion(attribute_value, get_column),
                    )
            return rewritten_criterion
        if not WindowFilterWrapping.term_reads_row(term):
            return term
        return get_column(term)

    @staticmethod
    def term_reads_row(term: Term) -> bool:
        """Whether a term's value depends on the row - a column, an aggregate, a window function
        or a correlated subquery - rather than being a constant.

        Args:
            term: The term.

        Returns:
            True when the term reads the row.
        """
        boundary_node_ids: set[int] = set()
        nodes: Iterator[Any] = term.nodes_()
        for node in nodes:
            if id(node) in boundary_node_ids:
                continue
            if node.is_subquery or isinstance(node, Selectable):
                if getattr(node, "outer_reference_terms", ()):
                    return True
                boundary_node_ids.update(map(id, node.nodes_()))
            elif isinstance(node, (Field, AggregateFunction)) or node.is_analytic:
                return True
        return False
