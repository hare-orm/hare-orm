from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.constants import (
    AUTO_DESCRIPTION_MAX_LENGTH,
    INT16_MAX,
    INT16_MIN,
)
from hare.fields.data.numeric.small_int_field import SmallIntField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class IntEnumFieldInstance(SmallIntField[int]):
    enum_type: type[IntEnum]

    def __init__(
        self,
        enum_type: type[IntEnum],
        description: str | None = None,
        generated: bool = False,
        **kwargs: Any,
    ) -> None:
        # Validate values
        minimum = 1 if generated else INT16_MIN
        for item in enum_type:
            try:
                value = int(item.value)
            except ValueError:
                raise ConfigurationError("IntEnumField only supports integer enums!")
            if not minimum <= value <= INT16_MAX:
                raise ConfigurationError(f"The valid range of IntEnumField's values is {minimum}..{INT16_MAX}!")

        # Automatic description for the field if not specified by the user
        if description is None:
            description = "\n".join([f"{e.name}: {int(e.value)}" for e in enum_type])[:AUTO_DESCRIPTION_MAX_LENGTH]

        super().__init__(description=description, **kwargs)
        self.enum_type = enum_type

    def get_assign_normalized_types(self) -> frozenset[type]:
        return frozenset({self.enum_type})

    def to_python(self, value: int | None) -> IntEnum | None:
        if value is None:
            return None
        try:
            return self.enum_type(value)
        except ValueError as exc:
            validation_error = self.get_validation_error(exc, value)
        raise validation_error

    def to_db_value(self, value: IntEnum | None | int, instance: type[Model] | Model) -> int | None:
        validation_error = None
        try:
            if isinstance(value, self.enum_type):
                # Already a member of this field's own enum type - its value is valid by
                # construction, no need to round-trip back through self.enum_type(...) just to
                # re-validate membership.
                value = int(value.value)
            elif isinstance(value, IntEnum):
                value = int(self.enum_type(int(value.value)))
            elif isinstance(value, int):
                value = int(self.enum_type(value))
        except ValueError as exc:
            # A non-member raises ValidationError, not a bare ValueError.
            validation_error = self.get_validation_error(exc, value)
        if validation_error is not None:
            raise validation_error
        self.validate(value)
        return value
