from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.enums import Arithmetic
from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table


class ArithmeticExpression(Term):
    """Wrapper for an arithmetic expression - simple with two terms, or complex with nested terms.

    Order of operations is preserved.
    """

    add_order = [Arithmetic.ADD, Arithmetic.SUB]

    def __init__(self, operator: Arithmetic, left: Any, right: Any, alias: str | None = None) -> None:
        """
        Args:
            operator: An operator for the expression, such as "+" or "/".
            left: The term on the left side of the expression.
            right: The term on the right side of the expression.
            alias: Optional alias for the term, usable inside a select statement.
        """
        super().__init__(alias)
        self.operator = operator
        self.left = left
        self.right = right

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.left.nodes_()
        yield from self.right.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        # True if both left and right terms are True or None. None if both terms are None. Otherwise, False
        return Term.get_combined_is_aggregate([self.left.is_aggregate, self.right.is_aggregate])

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.left = self.left.replace_table(current_table, new_table)
        self.right = self.right.replace_table(current_table, new_table)

    def left_needs_parens(self, curr_op: Arithmetic, left_op: Arithmetic | None) -> bool:
        """Returns true if the expression on the left of the current operator needs to be enclosed in parentheses.

        Args:
            curr_op: The current operator.
            left_op: The highest level operator of the left expression.
        """
        if left_op is None:
            # If the left expression is a single item.
            return False
        if curr_op in self.add_order:
            # If the current operator is '+' or '-'.
            return False
        # '*' or '/' after '+' or '-' needs parentheses: (A + B) / ...
        return left_op in self.add_order

    def right_needs_parens(self, curr_op: Arithmetic, right_op: Arithmetic | None) -> bool:
        """Returns true if the expression on the right of the current operator needs to be enclosed in parentheses.

        Args:
            curr_op: The current operator.
            right_op: The highest level operator of the right expression.
        """
        if right_op is None:
            # If the right expression is a single item.
            return False
        if curr_op == Arithmetic.ADD:
            return False
        if curr_op == Arithmetic.DIV:
            return True
        if curr_op == Arithmetic.MUL and right_op == Arithmetic.DIV:
            # a * (b / c) must stay parenthesized - rendering it as "a*b/c" evaluates
            # left-to-right as (a*b)/c, which diverges from the constructed a*(b/c) under
            # integer division (e.g. a=3,b=2,c=4: a*(b/c)=3*0=0 but (a*b)/c=6/4=1).
            return True
        # A '+' or '-' on the right of '*', '/' or '-' needs parentheses: ... - (A + B)
        return right_op in self.add_order

    def get_sql(self, ctx: SqlContext) -> str:
        left_op, right_op = [getattr(side, "operator", None) for side in [self.left, self.right]]

        arithmetic_sql = "{left}{operator}{right}".format(
            operator=self.operator,
            left=("({})" if self.left_needs_parens(self.operator, left_op) else "{}").format(self.left.get_sql(ctx)),
            right=("({})" if self.right_needs_parens(self.operator, right_op) else "{}").format(
                self.right.get_sql(ctx)
            ),
        )

        if ctx.with_alias:
            return ctx.format_alias_sql(arithmetic_sql, self.alias)

        return arithmetic_sql
