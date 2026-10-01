from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.query.expressions import Expression, F
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.sql.terms.functions.analytic_function import AnalyticFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.expressions import ExpressionContext, ExpressionResult
    from hare.query.expressions.base.expression_result import TableCriterionTuple


class WindowFunction(Plannable, abstract=True):
    """Base for values usable as the ``expression`` of ``Window(...)``."""

    #: Whether the window gets an explicit ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    #: frame - the default frame of an ordered window ends at the current row.
    full_partition_frame: bool = False
    #: Whether the function only passes a field's stored value through (or counts it), so an
    #: encrypted field's ciphertext still decrypts to a meaningful result.
    accepts_encrypted_field: bool = False
    #: The field or expression the function reads - set by a subclass reading one.
    field: str | Expression

    def get_plan_options(self) -> tuple[Any, ...]:
        """Options baked into the function's SQL (not bound values) - part of the plan key."""
        return ()

    def get_plan_arguments(self, context: PlanContext) -> Iterable[PlanDescription | None]:
        """Describes the function's arguments in the order the build resolves them.

        Args:
            context: The context the function is resolved in.

        Returns:
            The descriptions - None for an argument keeping no plan.
        """
        return ()

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The function's class and options, then its arguments.

        Args:
            context: The context the function is resolved in.

        Returns:
            The description, None when an argument keeps no plan.
        """
        return PlanDescription.combine((type(self), self.get_plan_options()), self.get_plan_arguments(context))

    def _get_field_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes the field or expression the function reads.

        Args:
            context: The context the function is resolved in.

        Returns:
            The field's name with the values of the annotation it names, or the expression's
            description.
        """
        field = self.field
        if isinstance(field, str):
            return PlanDescription(field, Expression.get_referenced_annotation_values(field, context))
        return field.get_plan_description(context)

    def _resolve_field(self, expression_context: ExpressionContext) -> tuple[ExpressionResult, Field[Any] | None]:
        """Resolves the field or expression this window function reads, refusing an encrypted
        field - the database would compute over its ciphertext.

        Args:
            expression_context: The query's resolve context.

        Returns:
            The resolved field and the field describing its values.

        Raises:
            QueryError: The field is encrypted.
        """
        field_expression = F(self.field) if isinstance(self.field, str) else self.field
        field_result = field_expression.get_result(expression_context)
        value_field = (
            field_result.output_field  # type: ignore[call-overload]
            if isinstance(self.field, str)
            else field_expression.get_value_field(field_result)
        )
        self._raise_if_encrypted_field(value_field)
        return field_result, value_field

    def _raise_if_encrypted_field(self, field_object: "Field[Any] | None") -> None:
        """Rejects an encrypted field this window function can't compute over.

        Args:
            field_object: The field the function reads.

        Raises:
            FieldError: ``field_object`` is encrypted and the function computes over its value.
        """
        if not self.accepts_encrypted_field:
            EncryptedFieldMixin.raise_if_encrypted(field_object, f"{type(self).__name__}()")

    def build(
        self, expression_context: ExpressionContext
    ) -> tuple[AnalyticFunction, list[TableCriterionTuple], "Field[Any] | None"]:
        raise NotImplementedError()

    #: The SQL window function's name.
    function_name: ClassVar[str]
    #: The SQL class of the function - one taking a frame (``ROWS BETWEEN ...``) where it has one.
    analytic_class: ClassVar[type[AnalyticFunction]] = AnalyticFunction

    def get_analytic_term(self, *args: Any) -> AnalyticFunction:
        """The SQL window function over ``args``."""
        return self.analytic_class(self.function_name, *args)
