from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.constants import (
    MULTIRANGE_SPAN_PATH_FUNCTION,
    MULTIRANGE_SPAN_PATH_SEGMENT,
    RANGE_BOUND_PATH_FUNCTIONS,
    RANGE_FLAG_PATH_FUNCTIONS,
    RANGE_VALUE_LOOKUPS,
)
from hare.dialects.postgresql.fields.ranges.range import Range
from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class MultiRangeField(Field[list[Range[Any]]]):
    """Base of the multirange fields - a set of non-overlapping ranges in one column (PostgreSQL 14+).

    The Python value is a list of ``Range`` (or ``(lower, upper)`` tuples), held the way PostgreSQL
    stores it: empty ranges dropped, the rest sorted and overlapping or adjacent ones merged. Each
    range goes through the field's ``RANGE_FIELD_CLASS``, so its bounds are coerced and read like a
    ``RangeField``'s.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    holds_container_value = True

    field_type = list

    #: The range field each member of the multirange goes through.
    RANGE_FIELD_CLASS: ClassVar[type[RangeField]] = RangeField

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.range_field = self.RANGE_FIELD_CLASS()
        if self.sensitive:
            # A member's own error message must hide the member the same way.
            self.range_field.sensitive = True

    @classmethod
    def get_field_for_range_field(cls, range_field: Field[Any]) -> MultiRangeField:
        """The multirange field holding ranges of ``range_field``'s class - ``RangeAgg``'s result.

        Args:
            range_field: A range field.

        Returns:
            A new multirange field.

        Raises:
            ValidationError: ``range_field`` isn't a range field with a multirange type.
        """
        for field_class in cls.__subclasses__():
            if field_class.RANGE_FIELD_CLASS is type(range_field):
                return field_class()
        raise ValidationError(f"{type(range_field).__name__} has no multirange type")

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Every member needs its bounds coerced and the list merged.
        return frozenset()

    def get_python_type(self) -> Any:
        return list[Range]  # type: ignore[type-arg]

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the multirange lookups import the dialect package this module is part of.
        from hare.dialects.postgresql.lookups.multiranges.postgresql_multi_range_field_lookups import (
            PostgresqlMultiRangeFieldLookups,
        )

        return PostgresqlMultiRangeFieldLookups.get_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """A bound (``busy__startswith``/``busy__endswith``), a flag (``busy__isempty``,
        ``busy__lower_inc``, ...) or the smallest range holding every member (``busy__span``)."""
        # Local import: the SQL terms import the fields package.
        from hare.sql.terms.functions.function import Function

        if segment in RANGE_BOUND_PATH_FUNCTIONS:
            return partial(Function, RANGE_BOUND_PATH_FUNCTIONS[segment]), self.range_field.get_bound_field()
        if segment in RANGE_FLAG_PATH_FUNCTIONS:
            return partial(Function, segment), self.range_field.FLAG_FIELD  # type: ignore[call-overload]
        if segment == MULTIRANGE_SPAN_PATH_SEGMENT:
            return partial(Function, MULTIRANGE_SPAN_PATH_FUNCTION), self.range_field
        return None

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """Equality, containment and position take a range (a multirange of it alone); ``contains``
        also one value."""
        bound_type = self.range_field.get_bound_field().field_type
        if lookup in RANGE_VALUE_LOOKUPS:
            return LookupValueShape.RANGE, bound_type
        if lookup == Lookup.CONTAINS:
            return LookupValueShape.VALUE, bound_type
        return None

    @staticmethod
    def is_range_value(value: Any) -> bool:
        """Whether ``value`` stands for one range rather than a multirange - a ``Range`` or a
        ``(lower, upper)`` tuple of two bounds.

        Args:
            value: A filter or member value.

        Returns:
            True for one range.
        """
        if isinstance(value, Range):
            return True
        return (
            isinstance(value, tuple)
            and len(value) == 2
            and not any(isinstance(item, (Range, tuple, list)) for item in value)
        )

    def get_members(self, value: Any) -> list[Any]:
        """The members of a multirange value as given - a list, tuple or set of ranges, or the
        multirange's PostgreSQL text form.

        Args:
            value: The value.

        Returns:
            The members, each a ``Range``, a ``(lower, upper)`` tuple, range text or a driver's range.

        Raises:
            ValidationError: ``value`` is one range, or not a collection of ranges.
        """
        if isinstance(value, str):
            return self.split_range_texts(value)
        if self.is_range_value(value) or not isinstance(value, (list, tuple, set)):
            raise ValidationError(
                f"{self.model_field_name}: expected a list of ranges, got {self.get_value_for_message(value)}"
            )
        return list(value)

    def split_range_texts(self, text: str) -> list[str]:
        """Splits a multirange's PostgreSQL text form (``{[1,3),[5,7)}``) into its ranges' texts.

        Args:
            text: The multirange text.

        Returns:
            The text of each range.

        Raises:
            ValidationError: The text isn't a multirange.
        """
        body = text.strip()
        if body[:1] != "{" or body[-1:] != "}":
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(text)} is not a multirange value"
            )
        range_texts: list[str] = []
        characters: list[str] = []
        is_quoted = is_inside_range = False
        for character in body[1:-1]:
            if is_quoted:
                characters.append(character)
                if character == '"':
                    is_quoted = False
            elif character == '"':
                characters.append(character)
                is_quoted = True
            elif character == "," and not is_inside_range:
                range_texts.append("".join(characters).strip())
                characters = []
            else:
                if character in "[(":
                    is_inside_range = True
                elif character in "])":
                    is_inside_range = False
                characters.append(character)
        last_text = "".join(characters).strip()
        if last_text or range_texts:
            range_texts.append(last_text)
        return range_texts

    def get_member_error(self, error: ValidationError, index: int, member: Any) -> ValidationError:
        """The error of one member, naming this field and the member's position.

        Args:
            error: The range field's error.
            index: The member's position.
            member: The member.

        Returns:
            The error to raise.
        """
        # range_field is never registered on a model, so its own message carries no field name.
        message = str(error).removeprefix(f"{self.range_field.model_field_name or ''}: ")
        return self.get_validation_error(error, member, f"{self.model_field_name}[{index}]: {message}")

    def get_member_range(self, index: int, member: Any) -> Range[Any]:
        """One member as the range field holds it - bounds coerced and read, the range canonical.

        Args:
            index: The member's position, named in an error message.
            member: The member.

        Returns:
            The range.

        Raises:
            ValidationError: The member isn't a range the range field takes.
        """
        validation_error = None
        if member is None or not (
            self.is_range_value(member) or isinstance(member, str) or hasattr(member, "lower_inc")
        ):
            validation_error = ValidationError(f"{self.get_value_for_message(member)} is not a range")
        else:
            try:
                range_value = self.range_field.to_python(member)
            except ValidationError as error:
                validation_error = error
            else:
                return range_value  # type: ignore[return-value]
        raise self.get_member_error(validation_error, index, member)

    @staticmethod
    def get_lower_sort_key(range_value: Range[Any]) -> tuple[Any, ...]:
        """The key ranges are ordered by - an unbounded lower bound first, then by the lower bound,
        an inclusive one before an exclusive one.

        Args:
            range_value: A non-empty range.

        Returns:
            The key.
        """
        if range_value.lower is None:
            return (0,)
        return (1, range_value.lower, 0 if range_value.lower_inc else 1)

    @staticmethod
    def get_merged_range(first: Range[Any], second: Range[Any]) -> Range[Any] | None:
        """The union of two ranges ordered by their lower bound when they overlap or touch.

        Args:
            first: The range starting first.
            second: The range starting no earlier.

        Returns:
            The union, None when there is a gap between the ranges.
        """
        if first.upper is not None and second.lower is not None:
            if second.lower > first.upper:
                return None
            if second.lower == first.upper and not (first.upper_inc or second.lower_inc):
                return None
        if first.upper is None or second.upper is None:
            upper, upper_inc = None, False
        elif second.upper > first.upper:
            upper, upper_inc = second.upper, second.upper_inc
        elif second.upper == first.upper:
            upper, upper_inc = first.upper, first.upper_inc or second.upper_inc
        else:
            upper, upper_inc = first.upper, first.upper_inc
        return Range(lower=first.lower, upper=upper, lower_inc=first.lower_inc, upper_inc=upper_inc)

    def get_canonical_ranges(self, members: list[Any]) -> list[Range[Any]]:
        """The members the way PostgreSQL stores the multirange: empty ranges dropped, the rest
        sorted and overlapping or adjacent ones merged.

        Args:
            members: The members as given.

        Returns:
            The ranges.

        Raises:
            ValidationError: A member isn't a range the range field takes.
        """
        ranges = [self.get_member_range(index, member) for index, member in enumerate(members)]
        ranges = sorted(
            (range_value for range_value in ranges if not range_value.is_empty), key=self.get_lower_sort_key
        )
        merged_ranges: list[Range[Any]] = []
        for range_value in ranges:
            merged_range = self.get_merged_range(merged_ranges[-1], range_value) if merged_ranges else None
            if merged_range is None:
                merged_ranges.append(range_value)
            else:
                merged_ranges[-1] = merged_range
        return merged_ranges

    def to_python(self, value: Any) -> list[Range[Any]] | None:
        if value is None:
            return None
        return self.get_canonical_ranges(self.get_members(value))

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        self.validate(value)
        if value is None:
            return None
        db_ranges = []
        for index, member in enumerate(self.get_members(value)):
            range_value = self.get_member_range(index, member)
            db_ranges.append(self.range_field.to_db_value(range_value, instance))
        return db_ranges
