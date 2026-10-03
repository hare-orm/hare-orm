from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.fields.base.field import Field
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.sql.terms.base.term import Term
from hare.utils.class_path import ClassPath

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult


class Expression(Plannable, abstract=True):
    """
    Parent class for expressions
    """

    #: The arguments the expression was constructed with - see ``deconstruct()``.
    _constructor_arguments: tuple[tuple[Any, ...], dict[str, Any]]

    @staticmethod
    def get_referenced_annotation_values(name: str, context: PlanContext) -> list[Any]:
        """The values of the annotation a name refers to (``F("total")``, the filter key
        ``total__gte``, a JSON path ``summary__key``) - resolving the name resolves the annotation
        again and records its values again.

        Args:
            name: The name.
            context: The context the name is resolved in.

        Returns:
            The values, none when the name refers to no expression annotation or to one keeping no
            plan.
        """
        annotations = context.annotations
        if not annotations:
            return []
        referenced_name = name if name in annotations else name.partition("__")[0]
        annotation = annotations.get(referenced_name)
        if not isinstance(annotation, Expression):
            return []
        # Without the name itself - a circular reference fails while the query is built.
        remaining_annotations = {key: value for key, value in annotations.items() if key != referenced_name}
        description = annotation.get_plan_description(PlanContext(remaining_annotations))
        return description.values if description is not None else []

    @staticmethod
    def get_argument_plan_description(value: Any, context: PlanContext) -> PlanDescription | None:
        """Describes a value passed as an argument - a function's extra argument, a window
        function's, a ``When(then=...)``/``Case(default=...)`` branch: an expression by its own
        description, a literal by its type, bound itself.

        Args:
            value: The value.
            context: The context the value is resolved in.

        Returns:
            The description, None for a SQL term or for a None, list or tuple literal, which are
            not bound as a parameter.
        """
        # Imported here: the modules import each other.
        from hare.query.expressions.base.value import Value

        if isinstance(value, Expression):
            return value.get_plan_description(context)
        if value is None or isinstance(value, (Term, list, tuple)):
            return None
        return PlanDescription(("literal", Value.get_literal_structure(value)), [value])

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
        return wrapped_query.get_bound_to(
            expression_context.connection, expression_context.model, "Subquery(...)/Exists(...)"
        )
