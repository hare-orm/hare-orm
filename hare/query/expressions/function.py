from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import QueryError
from hare.fields.base.field import Field
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.fields.generated import GeneratedField
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.base.arithmetic_expression_mixin import ArithmeticExpressionMixin
from hare.query.expressions.base.combined_expression import CombinedExpression
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.base.value import Value
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.f import F
from hare.query.expressions.q import Q
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.functions.collate import Collate
from hare.sql.functions.declarations import NumericCast
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.function import Function as HareSqlFunction


class Function(ArithmeticExpressionMixin):
    """
    Function/Aggregate base.

    Args:
        field: Field name
        default_values: Extra parameters to the function.

    Attributes:
        database_func: The SQL function this represents.
        populate_field_object: Enable populate_field_object where we want to try and preserve
            the field type.
    """

    __slots__ = ("field", "field_object", "default_values")

    database_func: type[HareSqlFunction] = HareSqlFunction
    #: The SQL function's name - set, the function renders as ``NAME(arguments...)`` through
    #: ``database_func``, which then takes the name first. A function with a SQL shape of its own
    #: leaves it None and sets ``database_func`` to a class building that shape.
    function_name: ClassVar[str | None] = None
    # Enable populate_field_object where we want to try and preserve the field type.
    populate_field_object = False
    #: Whether the function returns one of its arguments as-is (Min/Max/Coalesce) and so keeps
    #: its explicit collation - any other function's result is compared by its own text.
    keeps_argument_collation = False

    def __init__(self, field: str | F | CombinedExpression | Function | Term, *default_values: Any) -> None:
        self.field = field
        self.field_object: Field[Any] | None = None
        self.default_values = default_values

    def _get_function_field(self, field: Term | str, *default_values) -> HareSqlFunction:
        if self.function_name is not None:
            return self.database_func(self.function_name, field, *default_values)
        return self.database_func(field, *default_values)  # type:ignore[arg-type]

    def _get_nested_field(self, expression_context: ExpressionContext, field: str) -> ExpressionResult:
        # A bare name of another annotation resolves like F() of it.
        if field in expression_context.annotations:
            annotation = expression_context.annotations[field]
            # `annotation is self` also catches the same expression registered under a second key
            # (values()/values_list() re-register an annotation under its positional alias).
            if annotation is not self and field not in expression_context.annotation_names_in_progress:
                expression_context.annotation_names_in_progress.add(field)
                annotation_output_field: Field[Any] | None = None
                try:
                    if isinstance(annotation, Term) and not isinstance(annotation, Expression):
                        term = annotation
                    else:
                        referenced_result = annotation.get_result(expression_context)
                        term = referenced_result.term
                        # The referenced annotation's own value type, as F(field) would decode it.
                        annotation_output_field = annotation.get_value_field(referenced_result)
                finally:
                    expression_context.annotation_names_in_progress.discard(field)
                if self.populate_field_object:
                    self.field_object = annotation_output_field
                return ExpressionResult(term=term, output_field=annotation_output_field)
            # The name is the annotation currently being resolved (annotate(price=Sum("price"))):
            # it can only mean the real model field of the same name; without one it is a cycle.
            if field.partition("__")[0] not in expression_context.model._meta.fields_map:
                raise QueryError(
                    f"Circular annotation reference detected: {field!r} depends on itself "
                    "(directly or through another annotation)"
                )
        if (tracker := AggregatedMultiValuedPaths.get_from(expression_context)) is not None:
            tracker.record_lookup(expression_context.model, field, expression_context.select_related_path_prefix)
        term, joins, output_field = LookupPaths.get_nested_field(
            expression_context.model,
            expression_context.table,
            field,
            visibility=expression_context.visibility,
            select_related_extra_conditions=expression_context.select_related_extra_conditions,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        if self.populate_field_object:
            self.field_object = output_field
        return ExpressionResult(term=term, joins=joins, output_field=output_field)

    def _get_argument(
        self, expression_context: ExpressionContext, value: Any, *, treat_str_as_field: bool
    ) -> ExpressionResult:
        if isinstance(value, Value):
            # A bool/Decimal/date/datetime literal decodes through its own type - Round(Value(
            # Decimal("1.555")), 2) is a Decimal, not SQLite's float.
            result = value.get_result(expression_context)
            return ExpressionResult(
                term=result.term, joins=result.joins, output_field=Value.get_literal_output_field(value.value)
            )
        if isinstance(value, Expression):
            # The type of the value the argument computes, not the field its own argument had -
            # Max(Length("name")) is an integer, not text.
            result = value.get_result(expression_context)
            return ExpressionResult(term=result.term, joins=result.joins, output_field=value.get_value_field(result))
        if isinstance(value, Term):
            return ExpressionResult(term=value)
        if isinstance(value, str) and treat_str_as_field:
            return self._get_nested_field(expression_context, value)
        if (encoder := Value.get_literal_encoder(value, expression_context.dialect)) is not None:
            # A timedelta/UUID/dict literal is bound in its field's stored form.
            return ExpressionResult(term=ValueWrapper(encoder(value)))
        return ExpressionResult(term=value)

    def _get_collation_argument(self, argument: ExpressionResult) -> ExpressionResult:
        """Drops an argument's explicit collation unless this function keeps it.

        Args:
            argument: The resolved argument.

        Returns:
            `argument`, or a copy whose term has no collation.
        """
        if self.keeps_argument_collation or not isinstance(argument.term, Collate):
            return argument
        return ExpressionResult(
            term=Collate.strip(argument.term),
            joins=argument.joins,
            output_field=argument.output_field,  # type:ignore[call-overload]
        )

    def _accepts_encrypted_argument(self) -> bool:
        """Whether this function gives a meaningful result over an encrypted field's ciphertext."""
        return False

    def _raise_if_encrypted_argument(self, argument: Any, argument_result: ExpressionResult) -> None:
        """Rejects an encrypted field as an argument of this function.

        Args:
            argument: The argument as given.
            argument_result: The argument's own resolve result.

        Raises:
            FieldError: The argument is an encrypted field this function can't work on.
        """
        if self._accepts_encrypted_argument():
            return
        field_object = (
            argument.get_value_field(argument_result)
            if isinstance(argument, Expression)
            else argument_result.output_field  # type:ignore[call-overload]
        )
        EncryptedFieldMixin.raise_if_encrypted(field_object, f"{type(self).__name__}()")

    @staticmethod
    def _cast_literal_argument(
        expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        """Types a bare literal main argument where the dialect says nothing around it does.

        Args:
            expression_context: Carries the model whose database decides the dialect.
            function_arg: The resolved main argument.

        Returns:
            `function_arg`, or a copy whose term is the cast literal.
        """
        term = function_arg.term
        if isinstance(term, Term) and not isinstance(term, ValueWrapper):
            return function_arg
        literal = term.value if isinstance(term, ValueWrapper) else term
        typed_term = Value.get_typed_term(
            term, literal, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect
        )
        if typed_term is term:
            return function_arg
        return ExpressionResult(
            term=typed_term,
            joins=function_arg.joins,
            output_field=function_arg.output_field,  # type:ignore[call-overload]
        )

    def _get_function_term(self, function_arg: ExpressionResult, default_terms: list[Any]) -> HareSqlFunction:
        """Builds the SQL function over the resolved main argument and default terms.

        Args:
            function_arg: The resolved (and wrapped) main argument.
            default_terms: The default value terms.

        Returns:
            The SQL function term.
        """
        return self._get_function_field(function_arg.term, *default_terms)

    def _wrap_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        """Hook: changes the resolved main argument before it becomes the function's first SQL argument
        - whether it was a field name or an expression.
        """
        return function_arg

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the function for SQL generation.

        Args:
            expression_context: The context the function is resolved in.

        Returns:
            The term, the joins and the output field.
        """

        function_arg = self._get_collation_argument(
            self._get_argument(expression_context, self.field, treat_str_as_field=True)
        )
        self._raise_if_encrypted_argument(self.field, function_arg)
        function_arg = self._cast_literal_argument(expression_context, function_arg)
        function_arg = self._wrap_argument(expression_context, function_arg)
        default_results = [
            self._get_collation_argument(self._get_argument(expression_context, value, treat_str_as_field=False))
            for value in self.default_values
        ]
        for default_value, default_result in zip(self.default_values, default_results, strict=True):
            self._raise_if_encrypted_argument(default_value, default_result)
        default_terms = self._get_default_terms(function_arg, default_results, expression_context)
        term = self._get_function_term(function_arg, default_terms)
        # A raw default value is wrapped inside the SQL function's constructor, not through Value -
        # recorded here. Skipped when the term's arguments aren't [field, *default_values].
        if expression_context.value_wrapper_refs is not None and len(term.args) == 1 + len(self.default_values):
            for original_default, wrapped_arg in zip(self.default_values, term.args[1:], strict=True):
                if isinstance(wrapped_arg, NumericCast):
                    wrapped_arg = wrapped_arg.args[0]
                if not isinstance(original_default, (Expression, Term)) and isinstance(wrapped_arg, ValueWrapper):
                    expression_context.value_wrapper_refs.append(
                        (
                            ValueRefOrigin.ANNOTATION,
                            LiteralValueRef(
                                wrapped_arg,
                                Value.get_literal_encoder(original_default, expression_context.dialect),
                            ),
                        )
                    )
        joins = function_arg.joins
        for value in default_results:
            if value.joins:
                joins = ExpressionResult.dedup_joins(joins, value.joins)
        res = ExpressionResult(
            term=term,
            joins=joins,
            output_field=self._get_output_field(function_arg, default_results),
        )

        if self.populate_field_object and (
            res_output_field := res.output_field  # type:ignore[call-overload]
        ):
            coerced_output_field = self._coerce_output_field(res_output_field)
            self.field_object = coerced_output_field
            # A new result carrying the coerced field - the query reads the output field off the
            # result.
            res = ExpressionResult(term=res.term, joins=res.joins, output_field=coerced_output_field)

        return res

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        return self._get_call_plan_description(context, None, None)

    def _get_call_plan_description(
        self, context: PlanContext, distinct: bool | None, filter_condition: Q | None
    ) -> PlanDescription | None:
        """Describes the call in the order the build resolves it: the main argument, an
        aggregate's ``_filter=`` condition, then the extra arguments - a literal one by its type,
        which decides a cast and the field the result is decoded through.

        Args:
            context: The context the call is resolved in.
            distinct: An aggregate's ``distinct=``, None for a function.
            filter_condition: An aggregate's ``_filter=`` condition.

        Returns:
            The description, None when an argument keeps no plan - a SQL term among them.
        """
        field = self.field
        if isinstance(field, Expression):
            field_description = field.get_plan_description(context)
        elif isinstance(field, str):
            field_description = PlanDescription(field, self.get_referenced_annotation_values(field, context))
        else:
            return None
        return PlanDescription.combine(
            (type(self), distinct, len(self.default_values), self.get_plan_options()),
            (
                field_description,
                PlanDescription.ABSENT if filter_condition is None else filter_condition.get_plan_description(context),
                *(self.get_argument_plan_description(default_value, context) for default_value in self.default_values),
            ),
        )

    def get_plan_options(self) -> tuple[Any, ...]:
        """Parameters baked into this function's SQL structure (not bound values) - part of the
        plan key.

        Returns:
            Nothing by default.
        """
        return ()

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        """A function's output field is its argument's field - the type of its own result only
        when ``populate_field_object`` opts in (Sum/Max/Min/Coalesce/...); Count("date_field")
        is an integer, whatever its argument's field.

        Args:
            result: This function's own resolve result.

        Returns:
            The output field when this function keeps its argument's type, else None.
        """
        if self.value_field is not None:
            return self.value_field
        return result.output_field if self.populate_field_object else None  # type:ignore[call-overload]

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        """Hook: the field of a result that isn't of the argument's type (``Avg()`` of an integer
        field). By default the argument's field.
        """
        return field_object

    @staticmethod
    def _get_effective_field_object(field_object: Field[Any]) -> Field[Any]:
        """Unwraps a ``GeneratedField`` to its ``output_field`` - the field whose type the value has.

        Args:
            field_object: A field.

        Returns:
            The wrapped field for a ``GeneratedField``, else ``field_object``.
        """
        return field_object.output_field if isinstance(field_object, GeneratedField) else field_object

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """Hook: the result's field when the default values take part in it (``Coalesce``). By default
        the main argument's field.
        """
        return function_arg.output_field  # type:ignore[call-overload]

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        """Hook for a subclass whose default_value TERMS need adjusting before they become SQL
        function arguments - see Coalesce's own override. Default: the resolved term, unchanged
        (every other Function's default_values pass straight through)."""
        return [value.term for value in default_results]
