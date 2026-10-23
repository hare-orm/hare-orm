from __future__ import annotations

import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.data.constants import INT32_MAX, INT32_MIN, INT_LOOKUP_LITERAL_CAST_SQL_TYPE
from hare.fields.enums import NativeWriteCheck
from hare.fields.field import Field
from hare.fields.narrowing.integer_range_limit import IntegerRangeLimit
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.validators.limits.max_value_validator import MaxValueValidator
from hare.fields.validators.limits.min_value_validator import MinValueValidator
from hare.query.enums import Lookup
from hare.sql import functions
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField

TInt = TypeVar("TInt", int, int | None)


class IntField(Field[TInt]):
    """A 32-bit signed integer - an ``INT`` column. A value outside the field's range is refused before it
    is written; as a primary key the database generates it unless ``generated=False``."""

    field_type = int

    SQL_TYPE = "INT"
    #: The values the column type itself holds - bounds equal to these are enforced by the column.
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = (INT32_MIN, INT32_MAX)
    allows_generated = True
    generated_requires_primary_key = True
    #: An int the driver returns is the value - to_python() only converts what is assigned.
    keeps_native_db_values = True

    @overload
    def __init__(
        self: IntField[int],
        primary_key: bool | None = None,
        *,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: IntField[int | None],
        primary_key: bool | None = None,
        *,
        null: Literal[True],
        **kwargs: Any,
    ) -> None: ...

    def __init__(self, primary_key: bool | None = None, **kwargs: Any) -> None:
        if primary_key or kwargs.get("pk"):
            kwargs["generated"] = bool(kwargs.get("generated", True))
        super().__init__(primary_key=primary_key, **kwargs)
        # The column type doesn't enforce the bounds on the Python side - validators do.
        bounds = self.constraints
        self.validators.append(MinValueValidator(bounds["ge"]))
        self.validators.append(MaxValueValidator(bounds["le"]))

    native_write_check = NativeWriteCheck.NOTHING

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        # A plain int - the common case - needs no check.
        if type(value) is not int and value is not None:
            self.raise_if_not_int(value)
        return super().to_db_value(value, instance)

    def raise_if_not_int(self, value: Any) -> None:
        """Refuses a value that isn't an int - a bool, a float, a text: int() would read "1_000", "٥" or
        5.7 as numbers the caller didn't mean.

        Args:
            value: The value, not None.

        Raises:
            ValidationError: ``value`` isn't an int.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{self.model_field_name}: expected an int, got {type(value).__name__}")

    @staticmethod
    def is_whole_number(value: float | Decimal) -> bool:
        """Whether a float/Decimal is finite and has no fractional part.

        Args:
            value: The number.

        Returns:
            True for e.g. ``5.0``, False for ``5.7``/NaN/Infinity.
        """
        if isinstance(value, float):
            return value.is_integer()
        return value.is_finite() and value == value.to_integral_value()

    def to_python(self, value: Any) -> Any:
        if type(value) is not int and value is not None:
            self.raise_if_not_int(value)
        return super().to_python(value)

    def from_db_value(self, value: Any) -> Any:
        # An integer expression a database computes as a fraction (a SUM of a wider type divided)
        # reads as the database gives it; a whole number as an int.
        if isinstance(value, (float, Decimal)) and not isinstance(value, bool) and math.isfinite(value):
            if value != int(value):
                return value
            value = int(value)
        elif isinstance(value, bool):
            value = int(value)
        elif isinstance(value, str):
            # A numeric text a column of no fixed type holds - read as it is, the check is for given values.
            value = Field.to_python(self, value)
        return self.to_python(value)

    def to_lookup_values(self, values: list[Any], instance: type[Model] | Model | None) -> list[Any]:
        # Plain ints - every key of a long `id__in` list, say - are checked in one pass.
        if type(self).to_lookup_value is IntField.to_lookup_value and type(self).to_db_value is IntField.to_db_value:
            validated_values = self.get_validated_values(values)
            if validated_values is not None:
                return validated_values
        return [self.to_lookup_value(value, instance) for value in values]

    def to_lookup_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        # Equality and membership take an int alone, as a write does - a comparison takes a fraction
        # too (get_compared_value()).
        return self.to_db_value(value, instance)

    def get_compared_value(self, value: Any, instance: type[Model] | Model, dialect: Dialect) -> Any:
        """The value a comparison (``gt``/``gte``/``lt``/``lte``/``range``) binds - a float or a
        Decimal too, a fraction cast to its own SQL type.

        Args:
            value: The value, not None.
            instance: The model (class) the filter is built for.
            dialect: The dialect the query runs on.

        Returns:
            The value or the cast fraction.

        Raises:
            ValidationError: ``value`` is NaN, past the field's bounds or no number.
        """
        if isinstance(value, (float, Decimal)):
            if value != value:
                # NaN - a value no write stores, compared differently by every database.
                raise ValidationError(f"{self.model_field_name}: NaN is no number to compare with")
            self.validate(value)
            if self.is_whole_number(value):
                return int(value)
            return functions.Cast(
                ValueWrapper(value), INT_LOOKUP_LITERAL_CAST_SQL_TYPE[Decimal if isinstance(value, Decimal) else float]
            )
        return dialect.types.get_lookup_value(self, value, instance)

    def encode_compared_value(self, value: Any, obj: Model | type[Model], field: Field[Any], dialect: Dialect) -> Any:
        """The value of ``gt``/``gte``/``lt``/``lte`` (``get_compared_value()``).

        Args:
            value: The value.
            obj: The model or object.
            field: The field.
            dialect: The dialect.

        Returns:
            The encoded value.
        """
        return None if value is None else self.get_compared_value(value, obj, dialect)

    def encode_compared_values(
        self, values: Any, obj: Model | type[Model], field: Field[Any], dialect: Dialect
    ) -> list[Any]:
        """The bounds of ``range`` (``get_compared_value()``), None for an open one.

        Args:
            values: The bounds.
            obj: The model or object.
            field: The field.
            dialect: The dialect.

        Returns:
            The encoded bounds.
        """
        # Local import: the filters import the fields.
        from hare.query.filters.lookups.value_encoders import ValueEncoders

        if not isinstance(values, (list, tuple, set)):
            # Refused as every list lookup refuses it.
            return ValueEncoders.encode_list(values, obj, field, dialect)
        return [None if value is None else self.get_compared_value(value, obj, dialect) for value in values]

    def get_lookups(self) -> dict[str, FieldLookup]:
        lookups = super().get_lookups()
        compared_lookups: dict[str, FieldLookup] = {
            suffix: lookups[suffix].with_changes(value_encoder=self.encode_compared_value)
            for suffix in (Lookup.GT, Lookup.GTE, Lookup.LT, Lookup.LTE)
        }
        return {
            **lookups,
            **compared_lookups,
            Lookup.RANGE: lookups[Lookup.RANGE].with_changes(value_encoder=self.encode_compared_values),
        }

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": INT32_MIN,
            "le": INT32_MAX,
        }

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        lowest, highest = self.COLUMN_TYPE_RANGE
        if isinstance(old_field, IntField):
            old_lowest, old_highest = old_field.COLUMN_TYPE_RANGE
            if old_lowest >= lowest and old_highest <= highest:
                return None
            return IntegerRangeLimit(lowest, highest, checks_fraction=False)
        if not isinstance(old_field, (FloatField, DecimalField)):
            return None
        # A fraction would be rounded away.
        return IntegerRangeLimit(lowest, highest, checks_fraction=True)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        # A primary key defaults to generated=True - only an opt-out is written.
        if self.pk:
            if self.generated:
                kwargs.pop("generated", None)
            else:
                kwargs["generated"] = False
        return path, args, kwargs
