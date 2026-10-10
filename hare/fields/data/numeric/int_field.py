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
from hare.sql import functions
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField

TInt = TypeVar("TInt", int, int | None)


class IntField(Field[TInt]):
    """
    Integer field. (32-bit signed)

    ``primary_key`` (bool):
        True if field is Primary Key.
    """

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
        minimum_validator = MinValueValidator(bounds["ge"])
        maximum_validator = MaxValueValidator(bounds["le"])
        self.validators.append(minimum_validator)
        self.validators.append(maximum_validator)
        # A Positive* field narrows the lower bound below what its column type enforces.
        column_minimum, column_maximum = self.COLUMN_TYPE_RANGE
        if bounds["ge"] == column_minimum:
            self.validators_enforced_by_column_type.append(minimum_validator)
        if bounds["le"] == column_maximum:
            self.validators_enforced_by_column_type.append(maximum_validator)

    native_write_check = NativeWriteCheck.NOTHING

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        # A plain int - the common case - needs none of the checks below.
        if type(value) is int:
            return super().to_db_value(value, instance)
        # A bool is an int in Python, not in a database: an integer column is given the number.
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, (float, Decimal)):
            self.check_whole_number(value)
        return super().to_db_value(value, instance)

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

    def check_whole_number(self, value: float | Decimal) -> None:
        """Rejects a float/Decimal with a fractional part - int() would silently truncate it.

        Args:
            value: The value to write.

        Raises:
            ValidationError: ``value`` isn't a finite whole number.
        """
        if not self.is_whole_number(value):
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is not a whole number"
            )

    def to_python(self, value: Any) -> Any:
        # A bool is stored as its int; a float or Decimal only when it is a whole number.
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, (float, Decimal)):
            self.check_whole_number(value)
        return super().to_python(value)

    def from_db_value(self, value: Any) -> Any:
        # An integer expression a database computes as a fraction (PostgreSQL divides a SUM, a
        # numeric, without truncating) reads as SQLite's integer division gives it - truncated.
        if isinstance(value, (float, Decimal)) and not isinstance(value, bool) and math.isfinite(value):
            value = int(value)
        return self.to_python(value)

    def to_lookup_values(self, values: list[Any], instance: type[Model] | Model) -> list[Any]:
        # Plain ints - every key of a long `id__in` list, say - are checked in one pass.
        if type(self).to_lookup_value is IntField.to_lookup_value and type(self).to_db_value is IntField.to_db_value:
            validated_values = self.get_validated_values(values)
            if validated_values is not None:
                return validated_values
        return [self.to_lookup_value(value, instance) for value in values]

    def to_lookup_value(self, value: Any, instance: type[Model] | Model) -> Any:
        # A plain int - every key of an `id__in` list, say - needs none of the checks below.
        if type(value) is int:
            return self.to_db_value(value, instance)
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, (float, Decimal)):
            self.validate(value)
            if self.is_whole_number(value):
                return int(value)
            return functions.Cast(
                ValueWrapper(value), INT_LOOKUP_LITERAL_CAST_SQL_TYPE[Decimal if isinstance(value, Decimal) else float]
            )
        return self.to_db_value(value, instance)

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
