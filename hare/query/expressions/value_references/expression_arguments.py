from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.query.expressions.constants import NO_CAST_SQL_TYPES
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.plans.description.term_plan_descriptions import TermPlanDescriptions
from hare.query.plans.plan_origins import PlanOrigins
from hare.sql.functions.cast import Cast
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.enums import ParameterPosition
    from hare.query.expressions.expression import Expression
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.expressions.f import F
    from hare.query.expressions.raw_sql import RawSQL
    from hare.query.expressions.value import Value
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences


class ExpressionArguments:
    """How the build of an expression resolves an argument - the one way every expression does: an
    expression into its result, a SQL term built by hand as it is, any other literal into a bound
    parameter recorded under the origin of the argument (``PlanOrigins``) while the query records
    its plan."""

    #: Classes imported on their first read; their modules import this one.
    expression_class: ClassVar[DeferredImport[type[Expression]]] = DeferredImport(
        "hare.query.expressions.expression", "Expression"
    )
    value_class: ClassVar[DeferredImport[type[Value]]] = DeferredImport("hare.query.expressions.value", "Value")
    field_reference_class: ClassVar[DeferredImport[type[F]]] = DeferredImport("hare.query.expressions.f", "F")

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
        typed_by_encoder: bool = False,
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
            typed_by_encoder: Whether ``encoder`` writes a literal as a value of a field, whose type it
                then has - an integer isn't cast to a SQL type of its own, which may be narrower.

        Returns:
            The argument's result - a None as ``NULL``; unless bound whole, a list or tuple as the
            term ``wrap_constant()`` makes of it, neither recorded: a None is part of the structure, a
            list keeps no plan.
        """
        expression_class = ExpressionArguments.expression_class
        value_class = ExpressionArguments.value_class
        if isinstance(value, expression_class):
            if encoder is not None and isinstance(value, value_class):
                return value.get_encoded_result(expression_context, encoder)
            return value.get_result(expression_context)
        if isinstance(value, Term):
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
            if (
                type(value) is int
                and not (typed_by_encoder and encoder is not None)
                and (cast_sql_type := value_class.get_cast_sql_type(value, NO_CAST_SQL_TYPES))
            ):
                # An integer beyond the one a database reads a bare literal as.
                term = Cast(term, cast_sql_type)
            elif position is not None:
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
            return ExpressionArguments.field_reference_class(field).get_result(expression_context)
        return ExpressionArguments.get_result(owner, attribute, field, expression_context, index=index)

    @staticmethod
    def record_raw_sql_parameters(value_wrapper_references: RecordedValueReferences, raw_sql: RawSQL) -> None:
        """Record the wrapped parameters of a raw SQL expression in their original order.

        Args:
            value_wrapper_references: The references the query records.
            raw_sql: The expression holding the parameters.
        """
        for index, parameter in enumerate(raw_sql.parameters):
            ExpressionArguments.record_literal(value_wrapper_references, raw_sql, "parameters", parameter, index=index)

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
