from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.constants import (
    RANGE_BOUND_PATH_FUNCTIONS,
    RANGE_FLAG_PATH_FUNCTIONS,
    RANGE_READING_METHOD_NAMES,
    RANGE_VALUE_LOOKUPS,
)
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.fields.data.boolean_field import BooleanField
from hare.query.enums import Lookup, LookupValueShape
from hare.query.rows.enums import RangeBoundType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term
from hare.dialects.postgresql.fields.ranges.range import Range


class RangeField(Field[Range[Any]]):
    """Base of the range fields. The Python value is a ``Range``, or a ``(lower, upper)`` tuple
    standing for ``Range(lower, upper)``. ``to_db_value`` returns the ``Range`` itself - each driver
    adapts it right before binding.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    holds_container_value = True

    field_type = Range

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Even a Range needs its bounds coerced and a discrete range canonicalized.
        return frozenset()

    #: The field of a flag read from the range (``during__isempty``) - one shared instance for
    #: every query reading one, and for every statement plan holding it.
    FLAG_FIELD: ClassVar[BooleanField[Any]] = BooleanField()

    #: The Postgres type of one element of the range - a bare value of `__contains` is cast to it.
    ELEMENT_SQL_TYPE = ""

    def get_bound_field(self) -> Field[Any]:
        """The field a bound of this range is read through (``startswith``/``endswith``).

        Returns:
            A shared field of the range's element type.
        """
        raise NotImplementedError(f"{type(self).__name__} has no element type")

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the range lookups import the dialect package this module is part of.
        from hare.dialects.postgresql.lookups.ranges.postgresql_range_field_lookups import PostgresqlRangeFieldLookups

        return PostgresqlRangeFieldLookups.get_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """A bound (``during__startswith``/``during__endswith``) or a flag (``during__isempty``,
        ``during__lower_inc``, ...) of the range."""
        # Local import: the SQL terms import the fields package.
        from hare.sql.terms.functions.function import Function

        if segment in RANGE_BOUND_PATH_FUNCTIONS:
            return partial(Function, RANGE_BOUND_PATH_FUNCTIONS[segment]), self.get_bound_field()
        if segment in RANGE_FLAG_PATH_FUNCTIONS:
            return partial(Function, segment), self.FLAG_FIELD  # type: ignore[call-overload]
        return None

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """Equality, containment and position take a range; ``contains`` also one value."""
        bound_type = self.get_bound_field().field_type
        if lookup in RANGE_VALUE_LOOKUPS:
            return LookupValueShape.RANGE, bound_type
        if lookup == Lookup.CONTAINS:
            return LookupValueShape.VALUE, bound_type
        return None

    #: The distance between two neighbouring values of a discrete range (int/date) - Postgres
    #: stores such a range canonically as ``[lower, upper)``. None for a continuous range.
    DISCRETE_STEP: Any = None

    #: The bounds ``rust.native.rows`` reads a range of this class with - None leaves every value to
    #: ``from_db_value()``.
    NATIVE_BOUND_TYPE: ClassVar[RangeBoundType | None] = None

    def get_read_codec_specification(
        self, types: TypeRegistry, zone_name: str | None
    ) -> tuple[str, dict[str, Any]] | None:
        # The codec repeats the reading of the class declaring NATIVE_BOUND_TYPE - a subclass
        # reading otherwise keeps its own.
        bound_type = type(self).NATIVE_BOUND_TYPE
        if bound_type is None:
            return None
        declaring_class = next(klass for klass in type(self).__mro__ if "NATIVE_BOUND_TYPE" in klass.__dict__)
        for method_name in RANGE_READING_METHOD_NAMES:
            if getattr(type(self), method_name, None) is not getattr(declaring_class, method_name, None):
                return None
        # Imported here: the rows package imports the fields package.
        from hare.query.rows.enums import ReadCodecType
        from hare.query.rows.native.field_codecs import FieldCodecs

        zone, zone_is_utc = FieldCodecs.get_zone(zone_name)
        return ReadCodecType.RANGE, {
            "bound": bound_type,
            "step": self.DISCRETE_STEP,
            "range_type": Range,
            "use_timezone": zone_name is not None,
            "zone": zone,
            "zone_is_utc": zone_is_utc,
            "fallback": self.from_db_value,
        }

    def canonicalize(self, value: Range[Any]) -> Range[Any]:
        """Coerces both bounds and rewrites ``value`` the way Postgres stores it: a discrete range
        as ``[lower, upper)``, an unbounded side as exclusive, and a range holding no values as
        the empty range.

        Raises:
            ValidationError: A bound can't be coerced, or lower is greater than upper.
        """
        if value.is_empty:
            return Range(lower=None, upper=None, lower_inc=False, upper_inc=False, is_empty=True)
        lower = self.coerce_bound(value.lower)
        upper = self.coerce_bound(value.upper)
        lower_inc = value.lower_inc and lower is not None
        upper_inc = value.upper_inc and upper is not None
        validation_error = None
        try:
            if self.DISCRETE_STEP is not None:
                if lower is not None and not lower_inc:
                    lower, lower_inc = lower + self.DISCRETE_STEP, True
                if upper is not None and upper_inc:
                    upper, upper_inc = upper + self.DISCRETE_STEP, False
        except OverflowError as error:
            validation_error = self.get_validation_error(error, value)
        if validation_error is not None:
            raise validation_error
        if lower is not None and upper is not None:
            if lower > upper:
                raise ValidationError(
                    f"{self.model_field_name}: range lower bound {self.get_value_for_message(lower)} is greater "
                    f"than upper bound {self.get_value_for_message(upper)}"
                )
            if lower == upper and not (lower_inc and upper_inc):
                return Range(lower=None, upper=None, lower_inc=False, upper_inc=False, is_empty=True)
        return Range(lower=lower, upper=upper, lower_inc=lower_inc, upper_inc=upper_inc)

    def to_db_value(self, value: Range[Any] | tuple[Any, Any] | None, instance: type[Model] | Model) -> Any:
        self.validate(value)
        if value is None:
            return None
        if isinstance(value, Range):
            if value.is_empty:
                return value
            return Range(
                lower=self.coerce_bound(value.lower),
                upper=self.coerce_bound(value.upper),
                lower_inc=value.lower_inc,
                upper_inc=value.upper_inc,
            )
        try:
            lower, upper = value
        except (ValueError, TypeError) as error:
            # A value of another shape raises ValidationError, not a ValueError/TypeError.
            raise ValidationError(f"{self.model_field_name}: {error}")
        return Range(lower=self.coerce_bound(lower), upper=self.coerce_bound(upper))

    def coerce_bound(self, value: Any) -> Any:
        """Converts one bound other than None to the range's element type before it reaches the driver
        - a bound of a wrong type would otherwise be written as it is.
        """
        return value

    def parse_bound_text(self, text: str) -> Any:
        """Converts one bound of a range's Postgres text form - overridden by each concrete
        subclass below.

        Args:
            text: The unquoted bound text.

        Returns:
            The bound value.
        """
        return text

    @staticmethod
    def split_bound_texts(body: str) -> list[str | None]:
        """Splits the part of a range's Postgres text form between its brackets into its two
        bounds, unquoting a double-quoted bound.

        Args:
            body: E.g. ``1,5`` or ``"2024-01-01 00:00:00+00",``.

        Returns:
            The lower and upper bound texts, None for an unbounded side.
        """
        bound_texts: list[str | None] = []
        characters: list[str] = []
        is_quoted = was_quoted = False
        index = 0
        while index < len(body):
            character = body[index]
            if is_quoted:
                if character == "\\" and index + 1 < len(body):
                    index += 1
                    characters.append(body[index])
                elif character == '"' and body[index + 1 : index + 2] == '"':
                    index += 1
                    characters.append('"')
                elif character == '"':
                    is_quoted = False
                else:
                    characters.append(character)
            elif character == '"':
                is_quoted = was_quoted = True
            elif character == ",":
                bound_texts.append("".join(characters) if characters or was_quoted else None)
                characters, was_quoted = [], False
            else:
                characters.append(character)
            index += 1
        bound_texts.append("".join(characters) if characters or was_quoted else None)
        return bound_texts

    def parse_range_text(self, text: str) -> Range[Any]:
        """Parses a range's Postgres text form, e.g. ``[1,5)``, ``(,"2024-01-01 00:00:00+00"]`` or
        ``empty`` - how a range reaches Python inside a JSON value (``JSONBAgg``).

        Args:
            text: The range text.

        Returns:
            The range.

        Raises:
            ValidationError: The text isn't a range.
        """
        if text.strip().lower() == "empty":
            return Range(lower=None, upper=None, lower_inc=False, upper_inc=False, is_empty=True)
        bound_texts = self.split_bound_texts(text[1:-1]) if len(text) >= 3 else []
        if text[:1] not in "[(" or text[-1:] not in "])" or len(bound_texts) != 2:
            raise ValidationError(f"{self.model_field_name}: {self.get_value_for_message(text)} is not a range value")
        lower_text, upper_text = bound_texts
        try:
            lower = None if lower_text is None else self.parse_bound_text(lower_text)
            upper = None if upper_text is None else self.parse_bound_text(upper_text)
        except (ValueError, ArithmeticError) as error:
            shown_error = "" if self.sensitive else f": {error}"
            validation_error = self.get_validation_error(
                error,
                text,
                f"{self.model_field_name}: {self.get_value_for_message(text)} is not a range value{shown_error}",
            )
        else:
            return Range(lower=lower, upper=upper, lower_inc=text[0] == "[", upper_inc=text[-1] == "]")
        raise validation_error

    def get_range(self, value: Any) -> Range[Any] | None:
        if value is None or isinstance(value, Range):
            return value
        if isinstance(value, str):
            return self.parse_range_text(value)
        # A plain (lower, upper) 2-tuple - the same convenience form to_db_value accepts, but
        # reachable here too: construction (Model(...)/create(...)) runs every field through
        # from_db_value directly, never through to_db_value first.
        if isinstance(value, tuple):
            lower, upper = value
            return Range(lower=lower, upper=upper)
        # The driver's own range object, read by its attributes. isempty is carried over: an empty
        # range has the same None bounds as an unbounded one.
        return Range(
            lower=value.lower,
            upper=value.upper,
            lower_inc=value.lower_inc,
            upper_inc=value.upper_inc,
            is_empty=value.isempty,
        )

    def to_python(self, value: Any) -> Range[Any] | None:
        # Bounds are coerced and the range canonicalized, so memory holds what a read returns.
        range_value = self.get_range(value)
        if range_value is None:
            return None
        return self.canonicalize(range_value)
