from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.plans.description.term_plan_descriptions import TermPlanDescriptions
from hare.query.plans.plan_origins import PlanOrigins
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.enums import ParameterPosition
    from hare.query.expressions.expression import Expression
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.expressions.f import F
    from hare.query.expressions.value import Value
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences


class ExpressionArguments:
    """How the build of an expression resolves an argument - the one way every expression does: an
    expression into its result, a SQL term built by hand as it is, any other literal into a bound
    parameter recorded under the origin of the argument (``PlanOrigins``) while the query records
    its plan."""

    #: ``Expression`` and ``Value`` - the expressions import this module, so they are taken on the
    #: first argument rather than imported here; None until then.
    expression_classes: ClassVar[tuple[type[Expression], type[Value]] | None] = None
    #: ``F``, taken on the first field name the same way; None until then.
    field_reference_class: ClassVar[type[F] | None] = None

    @staticmethod
    def get_expression_classes() -> tuple[type[Expression], type[Value]]:
        """``Expression`` and ``Value``, kept on the class once taken.

        Returns:
            The classes.
        """
        from hare.query.expressions.expression import Expression
        from hare.query.expressions.value import Value

        ExpressionArguments.expression_classes = (Expression, Value)
        return Expression, Value

    @staticmethod
    def get_field_reference_class() -> type[F]:
        """``F``, kept on the class once taken.

        Returns:
            The class.
        """
        from hare.query.expressions.f import F

        ExpressionArguments.field_reference_class = F
        return F

    @staticmethod
    def get_result(
        owner: Any,
        attribute: str,
        value: Any,
        expression_context: ExpressionContext,
        *,
        encoder: Callable[[Any], Any] | None = None,
        position: ParameterPosition | None = None,
        index: int | None = None,
        binds_whole: bool = False,
    ) -> ExpressionResult:
        """Resolves an argument as its description (``PlanParts.describe_argument()``) lists it.

        Args:
            owner: The object the argument was passed to.
            attribute: The attribute holding it.
            value: The argument.
            expression_context: The context the argument is resolved in.
            encoder: Converts a literal - a ``Value``'s too - into its bound form first.
            position: Where a bound literal stands in the statement - it is cast where the dialect
                says nothing around it types the parameter. None to leave it uncast.
            index: Its position in a sequence held under ``attribute``.
            binds_whole: Whether a literal - a list or tuple too - is bound as one parameter, as an
                ``ENCODED_ARGUMENT`` part describes it.

        Returns:
            The argument's result - a None as ``NULL``; unless bound whole, a list or tuple as the
            term ``wrap_constant()`` makes of it, neither recorded: a None is part of the structure, a
            list keeps no plan.
        """
        expression_classes = ExpressionArguments.expression_classes or ExpressionArguments.get_expression_classes()
        expression_class, value_class = expression_classes
        if isinstance(value, expression_class):
            if encoder is not None and isinstance(value, value_class):
                return value.get_encoded_result(expression_context, encoder)
            return value.get_result(expression_context)
        if isinstance(value, Term) and not isinstance(value, ValueWrapper):
            # A SQL term built by hand binds its values as its description lists them.
            TermPlanDescriptions.record(value, expression_context.value_wrapper_references)
            return ExpressionResult(term=value)
        literal = value if encoder is None or value is None else encoder(value)
        term: Term = ValueWrapper(literal) if binds_whole and value is not None else Term.wrap_constant(literal)
        if isinstance(term, ValueWrapper):
            if expression_context.value_wrapper_references is not None:
                ExpressionArguments.record_literal(
                    expression_context.value_wrapper_references, owner, attribute, term, encoder, index
                )
            if position is not None:
                term = value_class.get_typed_term(term, term.value, position, expression_context.dialect)
        return ExpressionResult(term=term)

    @staticmethod
    def get_field_result(
        owner: Any, attribute: str, field: Any, expression_context: ExpressionContext, index: int | None = None
    ) -> ExpressionResult:
        """Resolves what an expression reads as its description (``PlanParts.describe_field()``)
        lists it: an expression into its result, a field or annotation name into the column it reads,
        a SQL term built by hand as it is.

        Args:
            owner: The expression.
            attribute: The attribute holding what it reads.
            field: What it reads.
            expression_context: The context the expression is resolved in.
            index: Its position in a sequence held under ``attribute``.

        Returns:
            The result.
        """
        if isinstance(field, str):
            field_reference_class = (
                ExpressionArguments.field_reference_class or ExpressionArguments.get_field_reference_class()
            )
            return field_reference_class(field).get_result(expression_context)
        return ExpressionArguments.get_result(owner, attribute, field, expression_context, index=index)

    @staticmethod
    def record_parameters(
        value_wrapper_references: RecordedValueReferences, owner: Any, attribute: str, parameters: list[ValueWrapper]
    ) -> None:
        """Records the parameters an object made when it was made, as a ``PARAMETERS`` part lists them.

        Args:
            value_wrapper_references: The references the query records.
            owner: The object.
            attribute: The attribute holding the parameters.
            parameters: The parameters.
        """
        for index, parameter in enumerate(parameters):
            ExpressionArguments.record_literal(value_wrapper_references, owner, attribute, parameter, index=index)

    @staticmethod
    def record_literal(
        value_wrapper_references: RecordedValueReferences,
        owner: Any,
        attribute: str,
        term: Any,
        encoder: Callable[[Any], Any] | None = None,
        index: int | None = None,
    ) -> None:
        """Records the reference of a literal argument - one whose term isn't a bound parameter (a
        value written into the text) can't be bound: the query keeps no plan.

        Args:
            value_wrapper_references: The references the query records.
            owner: The object the argument was passed to.
            attribute: The attribute holding it.
            term: The term the literal was resolved into.
            encoder: Converts a later value into its bound form, as the build converted this one.
            index: Its position in a sequence held under ``attribute``.
        """
        value_wrapper_references.append(
            (
                PlanOrigins.get_value_origin(owner, attribute, index),
                LiteralValueReference(term, encoder) if isinstance(term, ValueWrapper) else None,
            )
        )
