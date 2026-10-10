from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.classes.class_path import ClassPath
from hare.fields.field import Field
from hare.query.plans.description.plannable import Plannable
from hare.query.query_connection import QueryConnection

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult


class Expression(Plannable, abstract=True):
    """
    Parent class for expressions
    """

    #: The arguments the expression was constructed with - see ``deconstruct()``.
    _constructor_arguments: tuple[tuple[Any, ...], dict[str, Any]]

    unplanned_attributes = ("_constructor_arguments",)

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        """Creates the expression and keeps the arguments it was constructed with, for
        ``deconstruct()``.

        Args:
            *args: The constructor's positional arguments.
            **kwargs: The constructor's keyword arguments.

        Returns:
            The new expression.
        """
        expression = super().__new__(cls)
        expression._constructor_arguments = (args, kwargs)
        return expression

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Returns how a migration file rebuilds the expression - its class path and the arguments
        it was constructed with - so a migration keeps the expression itself, and the database a
        migration runs on renders its own SQL for it.

        Returns:
            The path, positional arguments and keyword arguments.
        """
        args, kwargs = self.__dict__.get("_constructor_arguments", ((), {}))
        return ClassPath.get(type(self)), list(args), dict(kwargs)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        raise NotImplementedError()

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
        return result.output_field  # type:ignore[call-overload]

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
