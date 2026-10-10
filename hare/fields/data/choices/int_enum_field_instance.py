from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.data.constants import AUTO_DESCRIPTION_MAX_LENGTH, INT16_MAX, INT16_MIN
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.field import Field
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
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
            description = "\n".join([f"{member.name}: {int(member.value)}" for member in enum_type])[
                :AUTO_DESCRIPTION_MAX_LENGTH
            ]

        super().__init__(description=description, **kwargs)
        self.enum_type = enum_type

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        """An enum losing a member another field held stores only its own members - checked where the
        column type names the members; else what a smaller integer needs."""
        if isinstance(old_field, IntEnumFieldInstance):
            limit = EnumValuesLimit.get_for_dropped_members(self.enum_type, old_field.enum_type, int)
            if limit is not None:
                return limit
        return super().get_narrowing_limit(old_field)

    def get_assign_normalized_types(self) -> frozenset[type]:
        return frozenset({self.enum_type})

    def to_python(self, value: int | None) -> IntEnum | None:
        if value is None:
            return None
        try:
            return self.enum_type(value)
        except ValueError as error:
            validation_error = self.get_validation_error(error, value)
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
        except ValueError as error:
            # A non-member raises ValidationError, not a bare ValueError.
            validation_error = self.get_validation_error(error, value)
        if validation_error is not None:
            raise validation_error
        self.validate(value)
        return value
