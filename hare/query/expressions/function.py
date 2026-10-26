from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import FieldError, QueryError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.arithmetic.combined_expression import CombinedExpression
from hare.query.expressions.compatibility.constants import TEXT_VALUE_TYPE
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.constants import FIELD_TYPED_LITERAL_TYPES, NO_CAST_SQL_TYPES
from hare.query.expressions.enums import TemporalType
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value import Value
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.term_plan_descriptions import TermPlanDescriptions
from hare.query.plans.enums import PlanPartType
from hare.sql.functions.cast import Cast
from hare.sql.functions.numeric_cast import NumericCast
from hare.sql.functions.text.collate import Collate
from hare.sql.terms.functions.function import Function as HareSqlFunction
from hare.sql.terms.term import Term
from hare.sql.terms.values.literal_value import LiteralValue
from hare.sql.terms.values.value_wrapper import ValueWrapper


class Function(CombinableExpression):
    """The base of the SQL functions of queries - and of the aggregates.

    Args:
        field: The first argument - a field name or an expression.
        default_values: The further arguments.

    Raises:
        QueryError: The number of arguments isn't one the function takes.
    """

    __slots__ = ("field", "field_object", "default_values")

    database_function: type[HareSqlFunction] = HareSqlFunction
    #: The SQL function's name - set, the function renders as ``NAME(arguments...)`` through
    #: ``database_function``, which then takes the name first. A function with a SQL shape of its own
    #: leaves it None and sets ``database_function`` to a class building that shape.
    function_name: ClassVar[str | None] = None
    #: Whether the result is read through the first argument's field (``Min``/``Max`` of a date reads a
    #: date) - else through the function's own output field.
    populate_field_object = False
    #: Whether the function returns one of its arguments as-is (Min/Max/Coalesce) and so keeps
    #: its explicit collation - any other function's result is compared by its own text.
    keeps_argument_collation = False
    #: The types of values the first argument can't be (``RESULT_VALUE_TYPE_FIELD_CLASSES``) -
    #: refused before the query runs: the result is read through the argument's field, which reads
    #: none of it (a sum of dates), or the function works on no such value.
    refused_argument_value_types: ClassVar[frozenset[str]] = frozenset()
    #: How many arguments the function takes after its first, at least and at most (None: any number).
    default_value_counts: ClassVar[tuple[int, int | None]] = (0, None)
    #: Whether the result is one of the arguments as given (Coalesce, Greatest, Least) - a literal among
    #: them may be the value written.
    returns_an_argument: ClassVar[bool] = False

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("get_plan_options", PlanPartType.KEY_METHOD),
        ("field", PlanPartType.FIELD),
        ("default_values", PlanPartType.ARGUMENTS),
        ("field_object", PlanPartType.NONE),
    )

    def __init__(self, field: str | F | CombinedExpression | Function | Term, *default_values: Any) -> None:
        least, most = self.default_value_counts
        if len(default_values) < least or (most is not None and len(default_values) > most):
            if most is None:
                expected = f"at least {least + 1}"
            elif least == most:
                expected = f"{least + 1}"
            else:
                expected = f"{least + 1} to {most + 1}"
            raise QueryError(f"{type(self).__name__}() takes {expected} argument(s), got {len(default_values) + 1}")
        self.field = field
        self.field_object: Field[Any] | None = None
        self.default_values = default_values

    def get_result_literals(self) -> list[Any]:
        if not self.returns_an_argument:
            return []
        # A first argument given as a str names a field.
        arguments = self.default_values if isinstance(self.field, str) else (self.field, *self.default_values)
        return [literal for argument in arguments for literal in self.get_literals_of(argument)]

    def _get_function_field(self, term: Term | str, *default_values: Any) -> HareSqlFunction:
        if self.function_name is not None:
            return self.database_function(self.function_name, term, *default_values)
        return self.database_function(term, *default_values)  # type:ignore[arg-type]

    def _get_nested_field(self, expression_context: ExpressionContext, field: str) -> ExpressionResult:
        main_name, __, path = field.partition("__")
        if isinstance(named_join := expression_context.annotations.get(main_name), NamedJoin):
            # A path through a named JOIN - a FilteredRelation's related field, a Lateral's column.
            result = named_join.get_path_result(path, expression_context)
            if self.populate_field_object:
                self.field_object = result.output_field
            return result
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
            # Decimal("1.555")), 2) is a Decimal, not the float a database may give.
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
        if type(value) is int and (cast_sql_type := Value.get_cast_sql_type(value, NO_CAST_SQL_TYPES)):
            # An integer beyond the one a database reads a bare literal as.
            return ExpressionResult(term=Cast(ValueWrapper(value), cast_sql_type))
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
            output_field=argument.output_field,
        )

    def _accepts_encrypted_argument(self) -> bool:
        """Whether this function gives a meaningful result over an encrypted field's ciphertext."""
        return False

    def _raise_if_argument_type_refused(self, argument: Any, argument_result: ExpressionResult) -> None:
        """Refuses an argument of a type of value the function doesn't take
        (``refused_argument_value_types``).

        Args:
            argument: The argument, as given.
            argument_result: Its resolved result.

        Raises:
            FieldError: The argument is of such a type.
        """
        if not self.refused_argument_value_types:
            return
        if isinstance(argument, Value):
            branch = ResultBranch(argument.value, None, is_literal=True)
        elif isinstance(argument, (str, Expression, Term)):
            # A string first argument names a field.
            branch = ResultBranch(None, argument_result.output_field, is_literal=False)
        else:
            branch = ResultBranch(argument, None, is_literal=True)
        value_type = OutputFieldCompatibility.get_value_type(branch)
        if value_type is None and branch.field is not None:
            value_type = GeneratedField.get_effective_field(branch.field).argument_value_type
        if value_type in self.refused_argument_value_types:
            raise FieldError(f"{type(self).__name__}() doesn't take {value_type} - convert it with Cast()")

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
            else argument_result.output_field
        )
        EncryptedFieldBase.raise_if_encrypted(field_object, f"{type(self).__name__}()")

    def _cast_literal_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult, argument: Any
    ) -> ExpressionResult:
        """Types a bare literal main argument where the dialect says nothing around it does - of a function
        returning one of its arguments, a literal bound in its field type's stored form
        (``FIELD_TYPED_LITERAL_TYPES``) as that type's column.

        Args:
            expression_context: Carries the model whose database decides the dialect.
            function_arg: The resolved main argument.
            argument: The main argument as given.

        Returns:
            `function_arg`, or a copy whose term is the cast literal.
        """
        term = function_arg.term
        if isinstance(term, Term) and not isinstance(term, ValueWrapper):
            return function_arg
        literal = term.value if isinstance(term, ValueWrapper) else term
        given_literal = argument.value if isinstance(argument, Value) else argument
        if (
            self.returns_an_argument
            and isinstance(given_literal, FIELD_TYPED_LITERAL_TYPES)
            and function_arg.output_field is not None
        ):
            # The text a UUID or a dict is bound as - as their column, or COALESCE() finds no type of both.
            cast_sql_type = expression_context.dialect.parameters.get_field_parameter_cast_type(
                function_arg.output_field
            )
            if cast_sql_type is None:
                return function_arg
            # The type as the dialect names it - Cast() writes a bare name upper-cased.
            return ExpressionResult(
                term=Cast(term, LiteralValue(cast_sql_type)),
                joins=function_arg.joins,
                output_field=function_arg.output_field,
            )
        typed_term = Value.get_typed_term(
            term, literal, ParameterPosition.FUNCTION_ARGUMENT, expression_context.dialect
        )
        if typed_term is term:
            return function_arg
        return ExpressionResult(
            term=typed_term,
            joins=function_arg.joins,
            output_field=function_arg.output_field,
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

    def _get_result_term(
        self,
        term: HareSqlFunction,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> Term:
        """Hook: the function's term once every argument is resolved.

        Args:
            term: The SQL function term.
            function_arg: The resolved main argument.
            default_results: The resolved default values.
            expression_context: The context the function is resolved in.

        Returns:
            The term itself by default.
        """
        return term

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
        if isinstance(self.field, Term) and not isinstance(self.field, Expression):
            # A SQL term built by hand binds its values as its description lists them.
            TermPlanDescriptions.record(self.field, expression_context.value_wrapper_references)
        self._raise_if_encrypted_argument(self.field, function_arg)
        self._raise_if_argument_type_refused(self.field, function_arg)
        function_arg = self._cast_literal_argument(expression_context, function_arg, self.field)
        if TEXT_VALUE_TYPE in self.refused_argument_value_types and NumericTyping.is_enum_field(
            function_arg.output_field
        ):
            # A function of numbers takes an integer enum's column by its number.
            function_arg.term = expression_context.dialect.renderers.get_enum_number_term(function_arg.term)
        function_arg = self._wrap_argument(expression_context, function_arg)
        default_results = [
            self._get_default_argument(expression_context, value, function_arg) for value in self.default_values
        ]
        function_arg = self._get_literal_beside_json_column(expression_context, function_arg, default_results)
        for default_value, default_result in zip(self.default_values, default_results, strict=True):
            self._raise_if_encrypted_argument(default_value, default_result)
        default_terms = self._get_default_terms(function_arg, default_results, expression_context)
        term = self._get_function_term(function_arg, default_terms)
        # A raw default value is wrapped inside the SQL function's constructor, not through Value -
        # recorded here. Skipped when the term's arguments aren't [field, *default_values].
        if expression_context.value_wrapper_references is not None and len(term.args) == 1 + len(self.default_values):
            for index, (original_default, wrapped_arg) in enumerate(
                zip(self.default_values, term.args[1:], strict=True)
            ):
                if isinstance(wrapped_arg, NumericCast | Cast):
                    wrapped_arg = wrapped_arg.args[0]
                if isinstance(original_default, Term) and not isinstance(original_default, Expression):
                    TermPlanDescriptions.record(original_default, expression_context.value_wrapper_references)
                elif not isinstance(original_default, (Expression, Term)) and isinstance(wrapped_arg, ValueWrapper):
                    ExpressionArguments.record_literal(
                        expression_context.value_wrapper_references,
                        self,
                        "default_values",
                        wrapped_arg,
                        self._get_default_literal_encoder(original_default, function_arg, expression_context),
                        index,
                    )
        term = self._get_result_term(term, function_arg, default_results, expression_context)
        joins = function_arg.joins
        for value in default_results:
            if value.joins:
                joins = ExpressionResult.dedup_joins(joins, value.joins)
        result = ExpressionResult(
            term=term,
            joins=joins,
            output_field=self._get_output_field(function_arg, default_results),
        )

        if self.populate_field_object and (result_output_field := result.output_field):
            coerced_output_field = self._coerce_output_field(result_output_field, expression_context)
            self.field_object = coerced_output_field
            # A new result carrying the coerced field - the query reads the output field off the
            # result.
            result = ExpressionResult(term=result.term, joins=result.joins, output_field=coerced_output_field)

        return result

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
        return result.output_field if self.populate_field_object else None

    def _coerce_output_field(self, field_object: Field[Any], expression_context: ExpressionContext) -> Field[Any]:
        """Hook: the field of a result that isn't of the argument's type (``Avg()`` of an integer
        field, the dialect's). By default the argument's field.
        """
        return field_object

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """Hook: the result's field when the default values take part in it (``Coalesce``). By default
        the main argument's field.
        """
        return function_arg.output_field

    def _get_default_argument(
        self, expression_context: ExpressionContext, value: Any, function_arg: ExpressionResult
    ) -> ExpressionResult:
        """Resolves one default value - a literal bound with ``_get_default_literal_encoder()``.

        Args:
            expression_context: The context the function is resolved in.
            value: The default value.
            function_arg: The resolved main argument.

        Returns:
            The resolved value.
        """
        if (
            not isinstance(value, (Expression, Term, str))
            and (encoder := self._get_default_literal_encoder(value, function_arg, expression_context)) is not None
        ):
            # A date/time/timedelta/UUID/dict literal is bound in its stored form.
            return ExpressionResult(term=ValueWrapper(encoder(value)))
        if isinstance(value, Value) and self.binds_as_main_field(value.value, function_arg):
            result = value.get_encoded_result(
                expression_context, self._get_default_literal_encoder(value.value, function_arg, expression_context)
            )
            return ExpressionResult(term=result.term, joins=result.joins)
        return self._get_collation_argument(self._get_argument(expression_context, value, treat_str_as_field=False))

    def _get_default_literal_encoder(
        self, value: Any, function_arg: ExpressionResult, expression_context: ExpressionContext
    ) -> Callable[[Any], Any] | None:
        """The conversion a literal default value is bound with - its own type's by default.

        Args:
            value: The literal.
            function_arg: The resolved main argument.
            expression_context: The context the function is resolved in.

        Returns:
            The encoder, None for a literal bound as it is - a naive datetime beside a wall clock in no
            zone (``NaiveDatetimeField``) is written as that field writes it, not as a moment of the
            configured zone, and a timedelta beside an interval as an interval.
        """
        if self.binds_as_main_field(value, function_arg):
            main_field: Field[Any] = function_arg.output_field  # type: ignore[assignment]
            return TemporalArithmetic.get_field_encoder(main_field, expression_context.dialect)
        return Value.get_literal_encoder(value, expression_context.dialect)

    def _get_literal_beside_json_column(
        self,
        expression_context: ExpressionContext,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
    ) -> ExpressionResult:
        """A literal main argument of a function returning one of its arguments, beside a JSON column among
        the default values, bound as that column's JSON - a text, a number or a list would be bound as itself,
        which the column then holds as no JSON, or a database finds no type of both.

        Args:
            expression_context: The context the function is resolved in.
            function_arg: The resolved main argument.
            default_results: The resolved default values.

        Returns:
            `function_arg`, or the literal encoded by the JSON column's field.

        Raises:
            ValidationError: The column's field refuses the literal.
        """
        if not self.returns_an_argument:
            return function_arg
        literal = self.field.value if isinstance(self.field, Value) else self.field
        if literal is None or isinstance(literal, (Expression, Term, str)) and not isinstance(self.field, Value):
            return function_arg
        json_field = next(
            (
                result.output_field
                for value, result in zip(self.default_values, default_results, strict=True)
                if not isinstance(value, Value) and Function.is_json_field(result.output_field)
            ),
            None,
        )
        if json_field is None or Function.is_json_field(function_arg.output_field):
            return function_arg
        encoded = TemporalArithmetic.get_field_encoder(json_field, expression_context.dialect)(literal)
        term: Term = ValueWrapper(encoded)
        if (
            cast_sql_type := expression_context.dialect.parameters.get_field_parameter_cast_type(json_field)
        ) is not None:
            term = Cast(term, LiteralValue(cast_sql_type))
        return ExpressionResult(term=term, joins=function_arg.joins, output_field=json_field)

    @staticmethod
    def is_json_field(field: Field[Any] | None) -> bool:
        """Whether a field holds JSON - a column a literal beside it is written into as JSON.

        Args:
            field: The field, None for none.
        """
        return field is not None and isinstance(GeneratedField.get_effective_field(field), JSONField)

    def binds_as_main_field(self, value: Any, function_arg: ExpressionResult) -> bool:
        """Whether a literal default value is bound as the main argument's field binds a value - see
        ``is_main_field_literal()``; of a function returning one of its arguments, any value beside a JSON
        column too, as its JSON.

        Args:
            value: The literal.
            function_arg: The resolved main argument.
        """
        if self.returns_an_argument and value is not None and Function.is_json_field(function_arg.output_field):
            return True
        return self.is_main_field_literal(value, function_arg)

    @staticmethod
    def is_main_field_literal(value: Any, function_arg: ExpressionResult) -> bool:
        """Whether a literal is bound as the main argument's field binds a value - a naive datetime beside a
        wall clock in no zone, a timedelta beside an interval.

        Args:
            value: The literal.
            function_arg: The resolved main argument.

        Returns:
            Whether the literal is bound as the main argument's field binds it.
        """
        if isinstance(value, datetime):
            return value.tzinfo is None and TemporalArithmetic.is_naive_field(function_arg.output_field)
        return (
            isinstance(value, timedelta)
            and TemporalArithmetic.get_field_temporal_type(function_arg.output_field) is TemporalType.INTERVAL
        )

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
