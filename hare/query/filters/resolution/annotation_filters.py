from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value_references.list_parameter_value_reference import ListParameterValueReference
from hare.query.expressions.value_references.list_value_reference import ListValueReference
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.filters.resolution.filter_values import FilterValues
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.lookups.field_lookup import FieldLookup


class AnnotationFilters:
    """Filters on an annotation of the queryset: the annotation a key starts with, its value reference,
    and the filter value encoded the way the annotation's field stores it."""

    @staticmethod
    def get_annotation_value_reference(
        criterion: Criterion, value: Any, parameter_converter: Callable[[Any], Any] | None
    ) -> LiteralValueReference | None:
        """The reference a later query binds the value of a filter on an annotation through - the
        one parameter holding the converted value, or the one parameter of the term it was
        converted into (a JSON path's JSON text cast to the JSON type).

        Args:
            criterion: The filter's criterion.
            value: The converted value it was built with.
            parameter_converter: How the given value was converted, None when it wasn't.

        Returns:
            The reference, None when the value isn't one parameter of the criterion.
        """
        if isinstance(value, Term):
            if parameter_converter is None or not any(node is value for node in criterion.find_(Term)):
                return None
            if len(value.find_(ValueWrapper)) != 1:
                return None
            return LiteralValueReference(
                value.find_(ValueWrapper)[0], partial(FilterValues.get_converted_term_parameter, parameter_converter)
            )
        value_wrappers = [node for node in criterion.find_(ValueWrapper) if node.value is value]
        if len(value_wrappers) != 1:
            return None
        return LiteralValueReference(value_wrappers[0], parameter_converter)

    @staticmethod
    def encode_annotation_filter_value(
        value_encoder: Callable[..., Any], model: Any, field: Field[Any] | None, dialect: Any, value: Any
    ) -> Any:
        """Runs a lookup's ``value_encoder`` on a value of a filter on an annotation.

        Args:
            value_encoder: The lookup's encoder.
            model: The model queried.
            field: The field the annotation resolves to.
            dialect: The dialect the query runs on.
            value: The value.

        Returns:
            The encoded value.
        """
        return value_encoder(value, model, field, dialect)

    @staticmethod
    def get_annotation_name(expression_context: ExpressionContext, key: str) -> str | None:
        """The annotation a filter key starts with.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Returns:
            The annotation's name - the longest one when several match - or None.
        """
        annotations = expression_context.annotations
        if not annotations:
            return None
        segments = key.split("__")
        for end in range(len(segments), 0, -1):
            name = "__".join(segments[:end])
            if name in annotations:
                return name
        return None

    @staticmethod
    def check_filterable(expression_context: ExpressionContext, key: str, annotation_info: ExpressionResult) -> None:
        """Raises for a filter on a window function where SQL rejects one.

        Args:
            expression_context: The context the filter resolves in.
            key: The filter key.
            annotation_info: The annotation's result.

        Raises:
            QueryError: The annotation is a window function and the query can't filter on one.
        """
        if (
            getattr(annotation_info.term, "is_analytic", False)
            and not expression_context.window_function_filter_allowed
        ):
            # A window function is only valid in SELECT/ORDER BY - SQL rejects it outright in
            # WHERE and HAVING. A .values()/.values_list() query applies such a filter to itself
            # wrapped as a derived table instead (window_function_filter_allowed).
            raise QueryError(
                f"Cannot filter on '{key}' - it is a window function (Window(...)) annotation, "
                "and SQL does not allow filtering on window functions directly. Filter a "
                ".values()/.values_list() query instead, which applies the filter to the query "
                "wrapped in a subquery, e.g. "
                # <pk field>, not 'pk': .values() doesn't take the `pk` alias.
                f"Model.objects.filter(pk__in=Subquery(queryset.filter({key}=...).values(<pk field>)))."
            )

    @staticmethod
    def get_compared_field(annotation: Any, annotation_info: ExpressionResult) -> Field[Any] | None:
        """The field whose lookups a filter on an annotation takes. A field's own lookups
        (containment of a range, array or JSON value) apply once the annotation's output field is
        known. An aggregate's output field is its argument's, so only an aggregate keeping a
        container or date/time value takes them.

        Args:
            annotation: The annotation.
            annotation_info: Its result.

        Returns:
            The field, None for the lookups of a value with no field.
        """
        annotation_output_field = annotation_info.output_field  # type: ignore[call-overload]
        value_field = annotation.get_value_field(annotation_info) if isinstance(annotation, Expression) else None
        if annotation_info.term.contains_aggregate:
            # An aggregate collecting values into an array/JSON/range value (ArrayAgg, JSONBAgg)
            # is filtered with that value's own lookups (`__contains`, `__overlap`, `__len`, ...);
            # one keeping its argument's date/time type (Max/Min) compares values of that type.
            if FilterValues.is_container_field(value_field) or FilterValues.is_date_or_time_field(value_field):
                return value_field
            return None
        if annotation_output_field is None and (
            FilterValues.is_date_or_time_field(value_field) or isinstance(value_field, TimeDeltaField)
        ):
            # A date/time/duration literal (Value(...)) compares values of its own type.
            return value_field
        return cast("Field[Any] | None", annotation_output_field)

    @staticmethod
    def get_parameter_converter(
        expression_context: ExpressionContext,
        field_lookup: FieldLookup,
        annotation_output_field: Field[Any] | None,
        value: Any,
    ) -> tuple[Callable[[Any], Any] | None, Field[Any] | None]:
        """How the value of a filter on an annotation becomes the bound one - the same conversion
        for a later value.

        Args:
            expression_context: The context the filter resolves in.
            field_lookup: The lookup.
            annotation_output_field: The field whose lookups the filter takes, None for none.
            value: The value given.

        Returns:
            The conversion, None for a value bound as it is; and the field the lookup's own
            encoder converts by, None when no encoder does.
        """
        if annotation_output_field is None or isinstance(value, Term):
            return None, None
        if field_lookup.value_encoder is not None:
            # A field-aware lookup takes an encoded value, as for a model field. The field-blind
            # __in/__isnull entries get the raw value.
            encoder_field = GeneratedField.get_effective_field(annotation_output_field)
            return partial(
                AnnotationFilters.encode_annotation_filter_value,
                field_lookup.value_encoder,
                expression_context.model,
                encoder_field,
                expression_context.dialect,
            ), encoder_field
        if FilterValues.is_container_field(annotation_output_field):
            # An array/JSON/range lookup with no value_encoder of its own (`__contains` of a
            # JSONBAgg) binds the value in the field's stored form, as the plain-field branch does.
            return partial(
                expression_context.dialect.types.get_lookup_value,
                annotation_output_field,
                instance=expression_context.model,
            ), None
        if isinstance(annotation_output_field, TimeDeltaField) and isinstance(value, timedelta):
            # A comparison operator with no value_encoder of its own binds the raw value - a
            # timedelta is stored as whole microseconds, and no driver accepts it as a parameter
            # for that column type (`.annotate(span=F("end") - F("start")).filter(span__gt=...)`).
            return partial(
                expression_context.dialect.types.get_db_value,
                TemporalArithmetic.TIMEDELTA_OUTPUT_FIELD,  # type: ignore[arg-type]
                instance=None,
            ), None
        if FilterValues.is_date_or_time_field(annotation_output_field) and value is not None:
            # A comparison operator with no value_encoder of its own binds the value as the
            # annotation's date/time field writes it - a naive datetime in the configured zone, a
            # date's string parsed, a naive time with the configured offset.
            return partial(
                expression_context.dialect.types.get_lookup_value,
                GeneratedField.get_effective_field(annotation_output_field),
                instance=expression_context.model,
            ), None
        return None, None

    @staticmethod
    def get_value_reference(
        expression_context: ExpressionContext,
        criterion: Criterion,
        operator: Callable[..., Any],
        raw_value: Any,
        value: Any,
        field_lookup: FieldLookup,
        encoder_field: Field[Any] | None,
        parameter_converter: Callable[[Any], Any] | None,
    ) -> LiteralValueReference | ListValueReference | ListParameterValueReference | None:
        """The reference a later query binds the value of a filter on an annotation through.

        Args:
            expression_context: The context the filter resolves in.
            criterion: The filter's criterion.
            operator: The operator that built it.
            raw_value: The value given.
            value: The value bound.
            field_lookup: The lookup.
            encoder_field: The field the lookup's own encoder converted the value by, None for none.
            parameter_converter: How the given value became the bound one, None for as it is.

        Returns:
            The reference, None when the value isn't a single parameter of a plain comparison.
        """
        if raw_value is None or isinstance(raw_value, (Term, Expression)):
            return None
        if not isinstance(raw_value, (list, tuple, set)):
            return AnnotationFilters.get_annotation_value_reference(criterion, value, parameter_converter)
        if field_lookup.value_encoder is None or encoder_field is None:
            return None
        list_reference = FilterValues.get_value_reference(
            expression_context, criterion, operator, raw_value, value, encoder_field, field_lookup.value_encoder
        )
        if isinstance(list_reference, (ListValueReference, ListParameterValueReference)):
            return list_reference
        return None
