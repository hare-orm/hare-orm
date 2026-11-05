from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import cast

from hare.query.statements.select.values.constants import WINDOW_FILTER_OPERAND_CRITERION_TYPES
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.field import Field
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.node import Node
from hare.sql.terms.node_structure import NodeStructure
from hare.sql.terms.node_traversal import NodeTraversal
from hare.sql.terms.term import Term


class WindowFilterWrapping:
    """A filter on a window function, which WHERE can't read: the query becomes a derived table and the
    filter is rewritten over its columns."""

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
        return cast(
            "Term", NodeStructure.rewrite(term, partial(WindowFilterWrapping.get_replacement, get_column=get_column))
        )

    @staticmethod
    def get_replacement(node: Node, *, get_column: Callable[[Term], Field]) -> Node | None:
        """Keep condition structure outside and project each row-dependent operand once."""
        if (
            isinstance(node, Criterion)
            and not isinstance(node, WINDOW_FILTER_OPERAND_CRITERION_TYPES)
            and not node.is_subquery
            and NodeStructure.get_attributes(node) is not None
        ):
            return None
        if isinstance(node, Term) and WindowFilterWrapping.term_reads_row(node):
            return get_column(node)
        return node

    @staticmethod
    def term_reads_row(term: Term) -> bool:
        """Whether a term's value depends on the row - a column, an aggregate, a window function
        or a correlated subquery - rather than being a constant.

        Args:
            term: The term.

        Returns:
            True when the term reads the row.
        """
        for node in NodeTraversal.walk(term, lambda child: not (child.is_subquery or isinstance(child, Selectable))):
            if node.is_subquery or isinstance(node, Selectable):
                if node.outer_reference_terms:
                    return True
            elif isinstance(node, (Field, AggregateFunction)) or node.is_analytic:
                return True
        return False
