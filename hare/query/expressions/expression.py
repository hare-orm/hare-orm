from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields.field import Field
from hare.query.expressions.structure.expression_declaration import ExpressionDeclaration
from hare.query.plans.description.plannable import Plannable
from hare.query.query_connection import QueryConnection
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.ordering import Ordering


class Expression(ExpressionDeclaration, Plannable, abstract=True):
    """The base of the expressions of a query - what resolves to a term of its SQL against a model."""

    declared_dialects: ClassVar[frozenset[type[Dialect]] | None] = None

    unplanned_attributes = ("_constructor_arguments",)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the expression against a query: its term, the joins it needs and the field its value is
        read by.

        Args:
            expression_context: The context of the query.

        Returns:
            The result.
        """
        raise NotImplementedError()

    def asc(self, *, nulls_first: bool = False, nulls_last: bool = False) -> Ordering:
        """Ascending ordering by this expression's value, for ``.order_by()``/``Meta.ordering``/
        ``Window(order_by=...)``/an ordered aggregate/an index's keys.

        Args:
            nulls_first: Place NULLs before every other value on any dialect.
            nulls_last: Place NULLs after every other value on any dialect.

        Raises:
            QueryError: If both flags are set.
        """
        return Ordering.build(self, is_ascending=True, nulls_first=nulls_first, nulls_last=nulls_last)

    def desc(self, *, nulls_first: bool = False, nulls_last: bool = False) -> Ordering:
        """Descending ordering by this expression's value - see ``asc()``.

        Args:
            nulls_first: Place NULLs before every other value on any dialect.
            nulls_last: Place NULLs after every other value on any dialect.

        Raises:
            QueryError: If both flags are set.
        """
        return Ordering.build(self, is_ascending=False, nulls_first=nulls_first, nulls_last=nulls_last)

    #: The field of the value an expression computes when it is always of one type - a count, an
    #: extracted date part; None when it follows the expression's arguments.
    value_field: ClassVar[Any] = None

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        """The field whose type the value this expression computes actually has - ``value_field``
        for an expression whose result always has one type.

        Args:
            result: This expression's own resolve result.

        Returns:
            The result's output field by default; a subclass whose output field only mirrors its
            argument's (and isn't the type of its own result) returns None instead.
        """
        if self.value_field is not None:
            return self.value_field
        return result.output_field

    def get_result_literals(self) -> list[Any]:
        """The literals the expression's value may be - none by default: the database computes it.

        Returns:
            The literals.
        """
        return []

    @staticmethod
    def get_literals_of(value: Any) -> list[Any]:
        """The literals a value given to an expression may be - a bare value itself, an expression's
        ``get_result_literals()``.

        Args:
            value: An argument, a branch.

        Returns:
            The literals.
        """
        if isinstance(value, Expression):
            return value.get_result_literals()
        # A SQL term is the database's to compute.
        return [] if isinstance(value, Term) else [value]

    @staticmethod
    def _get_wrapped_query(wrapped_query: AwaitableQuery[Any], expression_context: ExpressionContext) -> Any:
        """Returns the query a ``Subquery(...)``/``Exists(...)`` wraps, bound to the connection the
        outer query runs on.

        Args:
            wrapped_query: The wrapped query.
            expression_context: The context the outer query is compiled in.

        Returns:
            A copy of the wrapped query bound to the outer query's connection.

        Raises:
            QueryError: The wrapped query is pinned to another connection than the outer query's.
        """
        return QueryConnection.get_bound_to(
            wrapped_query, expression_context.connection, expression_context.model, "Subquery(...)/Exists(...)"
        )
