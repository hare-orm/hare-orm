from __future__ import annotations

import math
import operator
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import QueryError, UnSupportedError, ValidationError
from hare.query.enums import Lookup
from hare.query.filters.lookups.date_part_lookups import DatePartLookups
from hare.query.filters.lookups.json.json_filter_parser import JsonFilterParser
from hare.query.filters.lookups.json.json_lookups import JsonLookups
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class JsonFilterGuards:
    """The dialect-neutral part of the JSON ``__filter`` lookup: value-shape checks and the order
    they're applied in. A dialect subclass builds the terms.

    Attributes:
        OPERATORS_BY_KEYWORD: The operator of each lookup keyword a filter key may end with.
        DATE_PART_OPERATORS: The operators that always compare the stored value as a date/datetime.
        ORDERING_OPERATORS: The operators that compare the stored value as a number unless the
            filter value selects another type.
        TEXT_OPERATORS: The operators matching the stored string's text; their value must be a string.
        KEY_EXISTENCE_OPERATORS: The operators testing key existence at the path.
    """

    OPERATORS_BY_KEYWORD: dict[str, Callable[..., Criterion]] = {
        Lookup.NOT: Lookups.not_equal,
        Lookup.ISNULL: Lookups.is_null,
        Lookup.NOT_ISNULL: Lookups.not_null,
        Lookup.HAS_KEY: JsonLookups.has_key,
        Lookup.HAS_KEYS: JsonLookups.has_keys,
        Lookup.HAS_ANY_KEYS: JsonLookups.has_any_keys,
        Lookup.IN: Lookups.is_in,
        Lookup.NOT_IN: Lookups.not_in,
        Lookup.GTE: cast("Callable[..., Criterion]", operator.ge),
        Lookup.GT: cast("Callable[..., Criterion]", operator.gt),
        Lookup.LTE: cast("Callable[..., Criterion]", operator.le),
        Lookup.LT: cast("Callable[..., Criterion]", operator.lt),
        Lookup.RANGE: Lookups.between,
        Lookup.CONTAINS: Lookups.contains,
        Lookup.STARTSWITH: Lookups.starts_with,
        Lookup.ENDSWITH: Lookups.ends_with,
        Lookup.IEXACT: Lookups.insensitive_exact,
        Lookup.ICONTAINS: Lookups.insensitive_contains,
        Lookup.ISTARTSWITH: Lookups.insensitive_starts_with,
        Lookup.IENDSWITH: Lookups.insensitive_ends_with,
        "year": DatePartLookups.year_equal,
        "quarter": DatePartLookups.quarter_equal,
        "month": DatePartLookups.month_equal,
        "week": DatePartLookups.week_equal,
        "day": DatePartLookups.day_equal,
        "hour": DatePartLookups.hour_equal,
        "minute": DatePartLookups.minute_equal,
        "second": DatePartLookups.second_equal,
        "microsecond": DatePartLookups.microsecond_equal,
    }
    DATE_PART_OPERATORS: tuple[Callable[..., Criterion], ...] = (
        DatePartLookups.day_equal,
        DatePartLookups.hour_equal,
        DatePartLookups.microsecond_equal,
        DatePartLookups.minute_equal,
        DatePartLookups.month_equal,
        DatePartLookups.quarter_equal,
        DatePartLookups.second_equal,
        DatePartLookups.week_equal,
        DatePartLookups.year_equal,
    )
    ORDERING_OPERATORS: tuple[Callable[..., Criterion], ...] = (
        cast("Callable[..., Criterion]", operator.gt),
        cast("Callable[..., Criterion]", operator.ge),
        cast("Callable[..., Criterion]", operator.lt),
        cast("Callable[..., Criterion]", operator.le),
        Lookups.between,
    )
    TEXT_OPERATORS: tuple[Callable[..., Criterion], ...] = (
        Lookups.contains,
        Lookups.starts_with,
        Lookups.ends_with,
        Lookups.insensitive_exact,
        Lookups.insensitive_contains,
        Lookups.insensitive_starts_with,
        Lookups.insensitive_ends_with,
    )
    KEY_EXISTENCE_OPERATORS: tuple[Callable[..., Criterion], ...] = (
        JsonLookups.has_key,
        JsonLookups.has_keys,
        JsonLookups.has_any_keys,
    )

    def build(self, field_term: Term, value: dict[str, Any]) -> Criterion:
        """Builds the criterion of ``field__filter=value``.

        Raises:
            QueryError: ``value`` isn't a one-key dict, or its value doesn't fit the operator.
        """
        key_parts, filter_value, operator_ = JsonFilterParser.get_operator(value, self.OPERATORS_BY_KEYWORD)
        return self.create_criterion(key_parts, field_term, operator_, filter_value)

    @staticmethod
    def get_value_type(value: Any) -> str:
        """Names the comparison type a single filter value selects."""
        if isinstance(value, bool):
            return "boolean"
        if type(value) in (int, float, Decimal):
            return "number"
        if isinstance(value, (date, datetime)):
            return "datetime"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (dict, list, tuple)):
            return "container"
        if value is None:
            return "null"
        return type(value).__name__

    @staticmethod
    def get_non_null_items(value: Any) -> list[Any] | None:
        """The non-``None`` items of an ``in``/``not_in``/``range`` value.

        Returns:
            The items, or ``None`` when ``value`` isn't a list/tuple/set.
        """
        if not isinstance(value, (list, tuple, set)):
            return None
        return [item for item in value if item is not None]

    def is_value_of_type(self, value: Any, value_type: str) -> bool:
        """True for a bare value of ``value_type``, or a list/tuple/set whose non-``None`` items
        are all of it (at least one)."""
        non_null_items = self.get_non_null_items(value)
        if non_null_items is None:
            return self.get_value_type(value) == value_type
        return bool(non_null_items) and all(self.get_value_type(item) == value_type for item in non_null_items)

    @staticmethod
    def is_aware_datetime_value(value: Any) -> bool:
        """True for a timezone-aware datetime, or a list/tuple/set holding at least one."""
        items = value if isinstance(value, (list, tuple, set)) else (value,)
        return any(isinstance(item, datetime) and item.utcoffset() is not None for item in items)

    def get_operator_name(self, operator_: Callable[..., Any]) -> str:
        """The ``__filter`` keyword of an operator, ``exact`` for plain equality."""
        for keyword, keyword_operator in self.OPERATORS_BY_KEYWORD.items():
            if keyword_operator is operator_:
                return keyword
        return "exact"

    @staticmethod
    def get_key_list(lookup_name: str, value: Any) -> list[str]:
        """Validates the key list of ``has_keys``/``has_any_keys``.

        Raises:
            UnSupportedError: ``value`` isn't a list of strings.
        """
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
            raise UnSupportedError(
                f"{lookup_name} expects a list of string keys, got {ValueDescriptions.get_description(value)}"
            )
        if not all(isinstance(key, str) for key in value):
            raise UnSupportedError(
                f"{lookup_name} expects a list of string keys, got {ValueDescriptions.get_description(value)}"
            )
        return list(value)

    @staticmethod
    def get_key(value: Any) -> str:
        """Validates the key of ``has_key``.

        Raises:
            UnSupportedError: ``value`` isn't a string.
        """
        if not isinstance(value, str):
            raise UnSupportedError(f"has_key expects a string key, got {ValueDescriptions.get_description(value)}")
        return value

    def get_text_value(self, operator_: Callable[..., Any], value: Any) -> str:
        """The string a text operator (``contains``, ``istartswith``, ...) matches.

        Raises:
            UnSupportedError: ``value`` is ``None``.
            QueryError: ``value`` isn't a string.
        """
        operator_name = self.get_operator_name(operator_)
        if value is None:
            raise UnSupportedError(
                f"JSON __filter {operator_name}: None is not supported - use isnull to match a JSON null "
                "or a missing key."
            )
        if isinstance(value, Enum) and isinstance(value.value, str):
            value = value.value
        if not isinstance(value, str):
            raise QueryError(
                f"JSON __filter {operator_name} expects a string, got {ValueDescriptions.get_description(value)}"
            )
        return value

    def check_value_types(self, operator_: Callable[..., Any], value: Any) -> None:
        """Rejects an ``in``/``not_in`` list mixing comparison types, and a ``range`` bound that is an
        object or an array - a range compares scalars.

        Raises:
            QueryError: The list mixes e.g. numbers and strings, or a range bound is a container.
        """
        if operator_ is Lookups.between and isinstance(value, (list, tuple)):
            if any(isinstance(bound, (dict, list, tuple, set)) for bound in value):
                raise QueryError(f"JSON __filter range bounds are single values, got {value!r}")
            return
        if operator_ not in (Lookups.is_in, Lookups.not_in) or not isinstance(value, (list, tuple, set)):
            return
        value_types = {self.get_value_type(item) for item in value} - {"null"}
        if len(value_types) > 1:
            raise QueryError(f"JSON __filter in/not_in values must all be of one type, got {sorted(value_types)}")

    @staticmethod
    def raise_if_not_finite(value: Any) -> None:
        """Rejects a compared value holding a NaN or an infinity - JSON has no such number, and the
        databases compare it each their own way.

        Args:
            value: The value - a scalar, a list of values or a container.

        Raises:
            ValidationError: A float or Decimal in it isn't finite.
        """
        pending = [value]
        while pending:
            item = pending.pop()
            if (isinstance(item, float) and not math.isfinite(item)) or (
                isinstance(item, Decimal) and not item.is_finite()
            ):
                raise ValidationError(
                    f"value {ValueDescriptions.get_shown_value(item)} is not a finite number, which JSON can't store"
                )
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set)):
                pending.extend(item)

    def is_container_value(self, operator_: Callable[..., Any], value: Any) -> bool:
        """True when ``value`` is a JSON object/array (or an in/not_in list of them)."""
        if operator_ in (Lookups.is_in, Lookups.not_in) and isinstance(value, (list, tuple, set)):
            return self.is_value_of_type(value, "container")
        return operator_ is not Lookups.between and isinstance(value, (dict, list))

    def get_text_term(self, field_term: Term, key_parts: list[str | int]) -> Term:
        """The value at the path as text - NULL for a JSON ``null`` or a missing key."""
        return JsonFilterParser.get_field_path(field_term, key_parts)

    def container_criterion(
        self, field_term: Term, key_parts: list[str | int], operator_: Callable[..., Any], value: Any
    ) -> Criterion:
        """Compares the JSON value at the path with a JSON object/array value.

        Raises:
            QueryError: The operator can't compare a JSON object/array.
        """
        container_term = self.get_container_term(field_term, key_parts)
        if operator_ is operator.eq:
            return container_term == self.get_container_value(value)
        if operator_ is Lookups.not_equal:
            return container_term.is_distinct_from(self.get_container_value(value))
        if operator_ in (Lookups.is_in, Lookups.not_in):
            # is_container_value() only accepts a list holding at least one JSON object/array.
            non_null_items = [item for item in value if item is not None]
            any_matches = self.get_container_in_list(container_term, non_null_items)
            if len(non_null_items) != len(value):
                any_matches |= self.get_text_term(field_term, key_parts).isnull()
            return any_matches if operator_ is Lookups.is_in else any_matches.is_not_true()
        operator_name = self.get_operator_name(operator_)
        raise QueryError(f"JSON __filter can't apply {operator_name} to a JSON object/array value")

    def get_guarded_term(
        self, field_term: Term, key_parts: list[str | int], operator_: Callable[..., Any], value: Any
    ) -> tuple[Term, str] | None:
        """The type-guarded term a scalar comparison runs against, with its SQL type, or ``None``
        when the value selects no type (e.g. only ``None``) and the plain text is compared."""
        # A bool comparison needs a boolean guard even under an ordering operator (booleans are
        # ordered, false < true), so it's checked before the operator-based ones.
        if self.is_value_of_type(value, "boolean"):
            return self.type_guarded_term(field_term, key_parts, "boolean", "boolean"), "boolean"
        if operator_ in self.DATE_PART_OPERATORS or self.is_value_of_type(value, "datetime"):
            # JSON has no date type - a date is stored as a string. An aware value is compared as
            # timestamptz, keeping the stored string's own UTC offset; a naive one as timestamp.
            sql_cast_type = "timestamptz" if self.is_aware_datetime_value(value) else "timestamp"
            return self.type_guarded_term(field_term, key_parts, "string", sql_cast_type), sql_cast_type
        if self.is_value_of_type(value, "string"):
            # Checked before the ordering operators - `{"name__gt": "b"}` compares text.
            return self.type_guarded_term(field_term, key_parts, "string", None), "text"
        if operator_ in self.ORDERING_OPERATORS or self.is_value_of_type(value, "number"):
            return self.type_guarded_term(field_term, key_parts, "number", "numeric"), "numeric"
        return None

    def in_list_criterion(
        self,
        field_term: Term,
        key_parts: list[str | int],
        guarded_term: Term,
        sql_type: str,
        operator_: Callable[..., Any],
        value: list[Any] | tuple[Any, ...] | set[Any],
    ) -> Criterion:
        """``in``/``not_in`` over a list of typed values. ``None`` matches a JSON ``null`` or a
        missing key, every other value the type-guarded stored value."""
        non_null_items = [item for item in value if item is not None]
        if len(non_null_items) == len(value):
            if operator_ is Lookups.is_in:
                return self.get_scalar_in_list(guarded_term, non_null_items, sql_type)
            return self.get_scalar_not_in_list(guarded_term, non_null_items, sql_type)
        matches = (
            self.get_scalar_in_list(guarded_term, non_null_items, sql_type)
            | self.get_text_term(field_term, key_parts).isnull()
        )
        return matches if operator_ is Lookups.is_in else matches.is_not_true()

    def prepare_value(self, value: Any, sql_type: str) -> Any:
        """The filter value as the dialect binds it against a guarded term of ``sql_type``."""
        return value

    def create_criterion(
        self, key_parts: list[str | int], field_term: Term, operator_: Callable[..., Any], value: Any
    ) -> Criterion:
        """Builds the criterion of one JSON ``__filter`` entry.

        Raises:
            QueryError: The value's shape doesn't fit the operator.
        """
        if operator_ in self.KEY_EXISTENCE_OPERATORS:
            return self.key_existence_criterion(field_term, key_parts, operator_, value)

        self.check_value_types(operator_, value)
        self.raise_if_not_finite(value)
        if self.is_container_value(operator_, value):
            return self.container_criterion(field_term, key_parts, operator_, value)

        text_term = JsonFilterParser.get_field_path(field_term, key_parts)
        # __isnull/__not_isnull's bool value is a flag for the lookup itself, not a claim about
        # the stored value's type - no type guard.
        if operator_ in (Lookups.is_null, Lookups.not_null):
            return operator_(text_term, value)
        if operator_ in self.TEXT_OPERATORS:
            value = self.get_text_value(operator_, value)

        guarded = self.get_guarded_term(field_term, key_parts, operator_, value)
        if guarded is None:
            return operator_(text_term, value)
        guarded_term, sql_type = guarded
        value = self.prepare_value(value, sql_type)
        if operator_ in (Lookups.is_in, Lookups.not_in) and isinstance(value, (list, tuple, set)):
            return self.in_list_criterion(field_term, key_parts, guarded_term, sql_type, operator_, value)
        return operator_(guarded_term, value)

    def key_existence_criterion(
        self, field_term: Term, key_parts: list[str | int], operator_: Callable[..., Any], value: Any
    ) -> Criterion:
        """``has_key``/``has_keys``/``has_any_keys`` on the value at the path (the column itself
        for an empty path)."""
        raise NotImplementedError

    def type_guarded_term(
        self, field_term: Term, key_parts: list[str | int], json_type: str, sql_cast_type: str | None
    ) -> Term:
        """The value at the path, cast to ``sql_cast_type``, or NULL when the stored value isn't
        of ``json_type`` (``boolean``, ``number`` or ``string``)."""
        raise NotImplementedError

    def get_container_term(self, field_term: Term, key_parts: list[str | int]) -> Term:
        """The JSON object/array at the path, comparable with ``get_container_value()``."""
        raise NotImplementedError

    def get_container_value(self, value: Any) -> Any:
        """A JSON object/array filter value, comparable with ``get_container_term()``."""
        raise NotImplementedError

    def get_container_in_list(self, container_term: Term, values: list[Any]) -> Criterion:
        """``container_term`` equals one of the JSON object/array ``values``."""
        raise NotImplementedError

    def get_scalar_in_list(self, guarded_term: Term, values: list[Any], sql_type: str) -> Criterion:
        """``guarded_term IN values``."""
        raise NotImplementedError

    def get_scalar_not_in_list(self, guarded_term: Term, values: list[Any], sql_type: str) -> Criterion:
        """``guarded_term NOT IN values``."""
        raise NotImplementedError
