from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError, UnSupportedError
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.constants import PLAN_CACHE_MISS
from hare.query.expressions.constants import DATE_TIMESTAMP_COMPARISON_LOOKUPS, LIST_LOOKUP_SUFFIXES
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.expressions.value import Value
from hare.query.expressions.value_references.array_value_reference import ArrayValueReference
from hare.query.expressions.value_references.encoded_value_reference import EncodedValueReference
from hare.query.expressions.value_references.like_value_reference import LikeValueReference
from hare.query.expressions.value_references.list_parameter_value_reference import ListParameterValueReference
from hare.query.expressions.value_references.list_value_reference import ListValueReference
from hare.query.expressions.value_references.open_range_value_reference import OpenRangeValueReference
from hare.query.expressions.value_references.range_value_reference import RangeValueReference
from hare.query.expressions.value_references.scalar_value_reference import ScalarValueReference
from hare.query.expressions.value_references.value_reference_types import FilterValueReference, RecordedValueReferences
from hare.query.filters import Like, ValueEncoders
from hare.query.filters.lookups.lookups import Lookups
from hare.query.filters.resolution.composite_key_filters import CompositeKeyFilters
from hare.sql.functions.datetime.date_as_timestamp import DateAsTimestamp
from hare.sql.functions.datetime.timestamp_comparand import TimestampComparand
from hare.sql.terms.array import Array
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.between_criterion import BetweenCriterion
from hare.sql.terms.criteria.contains_criterion import ContainsCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.parameters.list_parameter import ListParameter
from hare.sql.terms.parameters.parameterized_value_wrapper import ParameterizedValueWrapper
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class FilterValues:
    """The value a filter compares with: converted by the field, bound as a parameter or written into
    the SQL, a subquery or an expression resolved, a date compared with a timestamp."""

    @staticmethod
    def get_value_reference(
        expression_context: ExpressionContext,
        criterion: Criterion,
        operator: Callable[..., Any],
        value: Any,
        encoded_value: Any,
        cache_reference_field: Field[Any],
        value_encoder: Callable[..., Any] | None,
    ) -> FilterValueReference | None:
        """The reference a later query binds a filter's value through - found by where the
        criterion holds the converted value.

        Args:
            expression_context: The context the filter is resolved in.
            criterion: The filter's criterion.
            operator: The operator that built it.
            value: The value given.
            encoded_value: The converted value the criterion embeds.
            cache_reference_field: The field the value was converted by.
            value_encoder: The lookup's value encoder, None for the field's own conversion.

        Returns:
            The reference, None when the value isn't where a reference can rebind it.
        """
        # Identity, not shape: a custom lookup may compare with a value derived from the encoded one
        # - a plan would then bind the wrong value.
        if value_encoder is None:
            return FilterValues.get_converted_value_reference(criterion, value, encoded_value, cache_reference_field)
        if isinstance(criterion, BetweenCriterion) or operator is Lookups.between:
            range_reference = FilterValues.get_range_value_reference(
                criterion, operator, encoded_value, cache_reference_field, value_encoder
            )
            # A BETWEEN binds through its bounds or not at all; a one-sided range may still be
            # bound as another lookup is.
            if range_reference is not None or isinstance(criterion, BetweenCriterion):
                return range_reference
        if isinstance(criterion, Like) and isinstance(operator, partial):
            like_reference = FilterValues.get_like_value_reference(
                criterion, operator, cache_reference_field, value_encoder
            )
            if like_reference is not None:
                return like_reference
        if isinstance(criterion, BasicCriterion):
            # An encoder returning a whole array literal: the encoder's result must be the right
            # operand itself - the Array node is then replaced as a whole.
            right = criterion.right
            if (
                right is encoded_value
                and isinstance(right, Function)
                and len(right.args) == 1
                and isinstance(right.args[0], Array)
            ):
                return ArrayValueReference(right.args[0], cache_reference_field, value_encoder)
            # Another encoded lookup ending as a plain comparison: the ValueWrapper must hold the
            # encoder's result itself, not a piece derived from it.
            if not isinstance(criterion, Like) and isinstance(right, ValueWrapper) and right.value is encoded_value:
                return EncodedValueReference(right, cache_reference_field, value_encoder)
        if not isinstance(value, (list, tuple, set)):
            # The encoded value as an argument of a function in the criterion (``UPPER(?)`` of
            # ``__iexact``, the text query of ``__search``, a JSON containment test): one parameter
            # holds the encoder's result itself.
            value_wrappers = [node for node in criterion.find_(ValueWrapper) if node.value is encoded_value]
            if len(value_wrappers) == 1:
                return EncodedValueReference(value_wrappers[0], cache_reference_field, value_encoder)
            return None
        return FilterValues.get_list_value_reference(
            expression_context, criterion, encoded_value, cache_reference_field, value_encoder
        )

    @staticmethod
    def get_converted_value_reference(
        criterion: Criterion, value: Any, encoded_value: Any, cache_reference_field: Field[Any]
    ) -> ScalarValueReference | None:
        """The reference of a value the field converted itself.

        Args:
            criterion: The filter's criterion.
            value: The value given.
            encoded_value: The converted value the criterion embeds.
            cache_reference_field: The field the value was converted by.

        Returns:
            The reference, None when the value isn't where a reference can rebind it.
        """
        if (
            isinstance(criterion, BasicCriterion)
            and isinstance(criterion.right, ValueWrapper)
            and criterion.right.value is encoded_value
        ):
            return ScalarValueReference(criterion.right, cache_reference_field)
        # The converted value as an argument of a function in the criterion (a JSON containment
        # test): one parameter holds it.
        value_wrappers = [node for node in criterion.find_(ValueWrapper) if node.value is encoded_value]
        if not isinstance(value, (list, tuple, set)) and len(value_wrappers) == 1:
            return ScalarValueReference(value_wrappers[0], cache_reference_field)
        return None

    @staticmethod
    def get_range_value_reference(
        criterion: Criterion,
        operator: Callable[..., Any],
        encoded_value: Any,
        cache_reference_field: Field[Any],
        value_encoder: Callable[..., Any],
    ) -> RangeValueReference | OpenRangeValueReference | None:
        """The reference of a range's bounds - both given as a BETWEEN, one given as a comparison
        with it alone. A bound must be the encoded bound itself.

        Args:
            criterion: The filter's criterion.
            operator: The operator that built it.
            encoded_value: The converted bounds.
            cache_reference_field: The field the value was converted by.
            value_encoder: The lookup's value encoder.

        Returns:
            The reference, None when the bounds aren't where a reference can rebind them.
        """
        if not isinstance(encoded_value, (list, tuple)) or len(encoded_value) != 2:
            return None
        range_encoder = None if value_encoder is ValueEncoders.encode_list else value_encoder
        start, end = encoded_value
        if isinstance(criterion, BetweenCriterion):
            if (
                isinstance(criterion.start, ValueWrapper)
                and isinstance(criterion.end, ValueWrapper)
                and criterion.start.value is start
                and criterion.end.value is end
            ):
                return RangeValueReference(criterion.start, criterion.end, cache_reference_field, range_encoder)
            return None
        if (
            operator is Lookups.between
            and isinstance(criterion, BasicCriterion)
            and isinstance(criterion.right, ValueWrapper)
            and (start is None) != (end is None)
            and criterion.right.value is (end if start is None else start)
        ):
            return OpenRangeValueReference(criterion.right, start is None, cache_reference_field, range_encoder)
        return None

    @staticmethod
    def get_like_value_reference(
        criterion: Like,
        operator: partial[Any],
        cache_reference_field: Field[Any],
        value_encoder: Callable[..., Any],
    ) -> LikeValueReference | None:
        """The reference of a LIKE lookup's pattern. The prefix and suffix flags are read off the
        partial the criterion was built with; a case-insensitive pattern sits inside UPPER(...) -
        the innermost ValueWrapper is referenced either way.

        Args:
            criterion: The filter's criterion.
            operator: The operator that built it.
            cache_reference_field: The field the value was converted by.
            value_encoder: The lookup's value encoder.

        Returns:
            The reference, None when the pattern isn't where a reference can rebind it.
        """
        case_insensitive = operator.keywords.get("case_insensitive")
        right = criterion.right
        pattern_wrapper: ValueWrapper | None = None
        if case_insensitive is False and isinstance(right, ValueWrapper):
            pattern_wrapper = right
        elif (
            case_insensitive is True
            and isinstance(right, Function)
            and len(right.args) == 1
            and isinstance(right.args[0], ValueWrapper)
        ):
            pattern_wrapper = right.args[0]
        if pattern_wrapper is None:
            return None
        return LikeValueReference(
            pattern_wrapper,
            cache_reference_field,
            value_encoder,
            operator.keywords["prefix"],
            operator.keywords["suffix"],
        )

    @staticmethod
    def get_list_value_reference(
        expression_context: ExpressionContext,
        criterion: Criterion,
        encoded_value: Any,
        cache_reference_field: Field[Any],
        value_encoder: Callable[..., Any],
    ) -> ListParameterValueReference | ListValueReference | None:
        """The reference of a list a filter compares with.

        Args:
            expression_context: The context the filter is resolved in.
            criterion: The filter's criterion.
            encoded_value: The converted list.
            cache_reference_field: The field the value was converted by.
            value_encoder: The lookup's value encoder.

        Returns:
            The reference, None when the list isn't where a reference can rebind it.
        """
        # A list the dialect binds as one parameter - any length binds through it.
        list_parameters = [node for node in criterion.find_(Term) if isinstance(node, ListParameter)]
        if len(list_parameters) == 1:
            return ListParameterValueReference(list_parameters[0], cache_reference_field, value_encoder)
        # __in/__not_in: one ContainsCriterion in the tree, alone or ORed with a NULL check. A None
        # in the list isn't bound - the plan key holds whether there is one. The container must be a
        # literal Tuple of values, not a subquery. Other encoded lookups keep no plan.
        contains_nodes = criterion.find_(ContainsCriterion)
        if len(contains_nodes) != 1 or not isinstance(contains_nodes[0].container, Tuple):
            return None
        container = contains_nodes[0].container
        container_values = container.values
        if not all(isinstance(container_value, ParameterizedValueWrapper) for container_value in container_values):
            return None
        parameterized_values = cast("list[ParameterizedValueWrapper]", container_values)
        # Identity again: the Tuple must wrap the encoded list's own elements.
        if not isinstance(encoded_value, (list, tuple, set)):
            return None
        bound_encoded_values = [item for item in encoded_value if item is not None]
        if len(bound_encoded_values) != len(parameterized_values):
            return None
        min_length = expression_context.dialect.parameters.single_parameter_in_list_min_length
        if min_length is not None and len(parameterized_values) >= min_length:
            # A long list the dialect couldn't bind as one parameter - its plan key holds no length.
            return None
        if not all(
            container_value.value is bound_encoded_value
            for container_value, bound_encoded_value in zip(parameterized_values, bound_encoded_values, strict=True)
        ):
            return None
        return ListValueReference(container, cache_reference_field, value_encoder)

    @staticmethod
    def get_filter_value(
        expression_context: ExpressionContext, key: str, value: Any
    ) -> tuple[Any, list[TableCriterionTuple], Field[Any] | None]:
        """The value a filter kwarg compares - a query value becomes a subquery, an expression
        value its term.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.

        Returns:
            The value, the joins an expression value adds, and the field an expression value reads.

        Raises:
            QueryError: An expression is an element of an ``__in``/``__not_in`` list.
        """
        value_field: Field[Any] | None = None
        filter_value = FilterValues.get_subquery_filter_value(value, expression_context.value_wrapper_references)
        filter_value_joins: list[TableCriterionTuple] = []
        if isinstance(filter_value, Subquery):
            filter_value.raise_if_selects_more_columns(
                key, CompositeKeyFilters.get_filter_column_count(expression_context.model, key)
            )
        if isinstance(filter_value, Expression):
            expression_result = filter_value.get_result(expression_context)
            EncryptedFieldBase.raise_if_compared_to_expression(
                expression_context.model,
                key,
                expression_result.output_field,  # type: ignore[call-overload]
            )
            filter_value = FilterValues.get_date_timestamp_comparand(
                expression_context,
                key,
                expression_result.term,
                expression_result.output_field,  # type: ignore[call-overload]
            )
            filter_value_joins = expression_result.joins
            value_field = expression_result.output_field  # type: ignore[call-overload]
        else:
            if isinstance(filter_value, (list, tuple, set)) and any(
                isinstance(element, Expression) for element in filter_value
            ):
                # An expression inside a literal list can't be resolved by the value encoder - a
                # field would stringify it or fail with a TypeError.
                raise QueryError(
                    f"'{key}' - an Expression (F()/Case()/...) can't be used as an element "
                    "of a __in/__not_in list. Combine per-value Q(...) objects with '|' instead, "
                    "e.g. Q(field=F(...)) | Q(field=other_value)."
                )

        return filter_value, filter_value_joins, value_field

    @staticmethod
    def get_subquery_filter_value(
        filter_value: Any, value_wrapper_references: RecordedValueReferences | None = None
    ) -> Any:
        """A query passed as a filter value, as the subquery the filter compares with
        (``field__in=Other.objects.filter(...)``). A bare queryset is narrowed to its model's
        primary key; a set operation to its combined rows' primary key.

        Args:
            filter_value: The filter value.
            value_wrapper_references: The references being recorded, None when no plan is.

        Returns:
            A ``Subquery``, the built SELECT of a set operation's primary key, or ``filter_value``
            unchanged.
        """
        # Deferred import: hare.query.queryset itself imports from this module at module level
        # (Q/Expression/...), so a direct import here would be circular.
        from hare.query.queryset import QuerySet
        from hare.query.statements import AwaitableQuery

        if not isinstance(filter_value, (QuerySet, AwaitableQuery)):
            return filter_value
        # Imported only for a query value - an ordinary filter value loads no set-operation module.
        from hare.query.statements.select.combined_query import CombinedQuery

        if isinstance(filter_value, QuerySet):
            # The values it selects, else its primary key - or the rows it combines.
            filter_value = filter_value._get_filter_value_compiler()
        if isinstance(filter_value, CombinedQuery) and not filter_value._combines_values:
            return filter_value._get_field_values_query("pk", value_wrapper_references)
        if isinstance(filter_value, AwaitableQuery):
            return Subquery(filter_value._get_filter_value_query())
        return filter_value

    @staticmethod
    def get_converted_term_parameter(parameter_converter: Callable[[Any], Any], value: Any) -> Any:
        """The one parameter of the term a value of a filter on an annotation converts into.

        Args:
            parameter_converter: The conversion.
            value: The value.

        Returns:
            The parameter's value; ``PLAN_CACHE_MISS`` - of no type a plan binds - when the value
            converts into anything else.
        """
        term = parameter_converter(value)
        if not isinstance(term, Term):
            return PLAN_CACHE_MISS
        value_wrappers = term.find_(ValueWrapper)
        return value_wrappers[0].value if len(value_wrappers) == 1 else PLAN_CACHE_MISS

    @staticmethod
    def get_date_timestamp_comparand(
        expression_context: ExpressionContext, key: str, term: Any, value_field: Field[Any] | None
    ) -> Any:
        """An expression value comparing a date with a timestamp - the date as the first moment of
        its day, as a date literal is (in the configured zone, a naive timestamp's wall clock).

        Args:
            expression_context: The context the kwarg is resolved against.
            key: The filter kwarg.
            term: The value's resolved term.
            value_field: The value's field.

        Returns:
            The term - a date promoted, a timestamp marked for the date column to be promoted.
        """
        field_name, __, lookup = key.partition("__")
        field_object = expression_context.model._meta.fields_map.get(field_name)
        if lookup not in DATE_TIMESTAMP_COMPARISON_LOOKUPS or field_object is None or value_field is None:
            return term
        filtered_field = GeneratedField.get_effective_field(field_object)
        compared_field = GeneratedField.get_effective_field(value_field)
        zone_name = Timezone.get_aware_zone_name()
        if isinstance(filtered_field, DatetimeField) and isinstance(compared_field, DateField):
            return DateAsTimestamp(term, zone_name)
        if isinstance(filtered_field, DateField) and isinstance(compared_field, DatetimeField):
            return TimestampComparand(term, zone_name)
        return term

    @staticmethod
    def is_date_or_time_field(field: Field[Any] | None) -> bool:
        """Whether a field holds a datetime, date or time value.

        Args:
            field: The field, possibly a GeneratedField, or None.

        Returns:
            True for a DatetimeField, DateField or TimeField.
        """
        return field is not None and isinstance(
            GeneratedField.get_effective_field(field), (DatetimeField, DateField, TimeField)
        )

    @staticmethod
    def is_container_field(field: Field[Any] | None) -> bool:
        """Whether a field holds an array, JSON or range value.

        Args:
            field: The field, possibly a GeneratedField, or None.

        Returns:
            True for a field whose ``holds_container_value`` is set.
        """
        effective_field = field.output_field if isinstance(field, GeneratedField) else field
        return effective_field is not None and effective_field.holds_container_value

    @staticmethod
    def holds_only_decimal_values(value: Any) -> bool:
        """Whether a filter value is a Decimal, or a list/tuple/set of Decimals and Nones.

        Args:
            value: The raw or encoded filter value.

        Returns:
            True when every bound value is a Decimal.
        """
        if isinstance(value, Decimal):
            return True
        return isinstance(value, (list, tuple, set)) and all(
            item is None or isinstance(item, Decimal) for item in value
        )

    @staticmethod
    def holds_decimal_value(value: Any) -> bool:
        """Whether a filter value is a Decimal, or a list/tuple/set holding one.

        Args:
            value: The raw or encoded filter value.

        Returns:
            True when a Decimal is bound for the comparison.
        """
        if isinstance(value, Decimal):
            return True
        return isinstance(value, (list, tuple, set)) and any(isinstance(item, Decimal) for item in value)

    @staticmethod
    def get_literal_value(value: Any) -> Any:
        """Unwraps a ``Value(...)`` filter value into its literal, so it is converted and validated
        by the filtered field like the same plain value.

        Args:
            value: The filter value.

        Returns:
            The literal, or ``value`` unchanged for anything else, ``Value(None)`` (a comparison
            with NULL, not an ``__isnull`` lookup) or a ``Value`` wrapping an expression.
        """
        if type(value) is not Value or value.value is None or isinstance(value.value, (Term, Expression)):
            return value
        return value.value

    @staticmethod
    def get_list_lookup_value(key: str, value: Any) -> Any:
        """Materializes an ``__in``/``__not_in`` value given as any iterable (a generator, a
        ``range``, a ``frozenset``, ``dict.keys()``, ...) into a list, consuming it exactly once.

        Args:
            key: The filter kwarg name.
            value: The filter value.

        Returns:
            The list, or ``value`` unchanged for any other key or a list/tuple/set, subquery or
            expression value.

        Raises:
            UnSupportedError: ``value`` is a string or bytes, which would otherwise be matched
                character by character.
        """
        if not key.endswith(LIST_LOOKUP_SUFFIXES) or isinstance(value, (list, tuple, set)):
            return value
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            raise UnSupportedError(
                f"{key}: expected an iterable of values, got a {type(value).__name__} - wrap a single value in a list"
            )
        from hare.models import Model

        if isinstance(value, Iterable) and not isinstance(value, (Term, Model)):
            return list(value)
        return value
