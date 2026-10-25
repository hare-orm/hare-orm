from __future__ import annotations

from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.constants import AUTO_DESCRIPTION_MAX_LENGTH, INT16_MAX, INT16_MIN
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.field import Field
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit

if TYPE_CHECKING:
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model


class IntEnumFieldInstance(SmallIntField[int]):
    """The field ``IntEnumField()`` makes: a member of an integer enum, stored as its value in a
    ``SMALLINT`` - read back as the member."""

    #: The enum whose members the field holds.
    enum_type: type[IntEnum]
    #: A migration declares the enum itself, its members as they are when it is written - the column's
    #: enum type or its CHECK holds them, and a member added to the class later is a change.
    snapshots_enum_in_migrations: ClassVar[bool] = True

    def __init__(
        self,
        enum_type: type[IntEnum],
        description: str | None = None,
        generated: bool = False,
        **kwargs: Any,
    ) -> None:
        if not (isinstance(enum_type, type) and issubclass(enum_type, Enum)):
            raise ConfigurationError(f"IntEnumField takes an enum class, got {enum_type!r}")
        # A generated column's values start at 1.
        minimum = 1 if generated else INT16_MIN
        for item in enum_type:
            try:
                value = int(item.value)
            except ValueError:
                raise ConfigurationError(
                    f"IntEnumField takes an enum of integers - {enum_type.__name__}.{item.name} is {item.value!r}"
                ) from None
            if not minimum <= value <= INT16_MAX:
                raise ConfigurationError(
                    f"IntEnumField values are {minimum} to {INT16_MAX} - {enum_type.__name__}.{item.name} is {value}"
                )

        # The column's description lists the members, unless one is given.
        if description is None:
            description = "\n".join([f"{member.name}: {int(member.value)}" for member in enum_type])[
                :AUTO_DESCRIPTION_MAX_LENGTH
            ]

        super().__init__(description=description, **kwargs)
        self.enum_type = enum_type

    def to_lookup_value(self, value: Any, instance: type[Model] | Model | None) -> int | None:
        # A number of no member - 7, 1.5 - is refused in a filter as in a write: no row holds it.
        return self.to_db_value(value, instance)

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        """An enum losing a member another field held, or an integer column becoming the enum's, stores only
        its own members; else what a smaller integer needs."""
        if isinstance(old_field, IntEnumFieldInstance):
            limit = EnumValuesLimit.get_for_dropped_members(self.enum_type, old_field.enum_type, int)
            if limit is not None:
                return limit
        elif isinstance(old_field, IntField):
            # A number of no member would be read as none.
            return EnumValuesLimit(frozenset(self.get_stored_member_values()))
        return super().get_narrowing_limit(old_field)

    def get_assign_normalized_types(self) -> frozenset[type]:
        return frozenset({self.enum_type})

    def to_python(self, value: int | None) -> IntEnum | None:
        if value is None:
            return None
        if not isinstance(value, self.enum_type):
            self.raise_if_not_int(value)
        try:
            return self.enum_type(value)
        except ValueError as error:
            validation_error = self.get_validation_error(error, value)
        raise validation_error

    def get_stored_member_values(self) -> tuple[int, ...]:
        """The numbers the members are stored as."""
        return tuple(int(member.value) for member in self.enum_type)

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> int | None:
        validation_error = None
        try:
            if isinstance(value, self.enum_type):
                # Already a member of this field's own enum type - its value is valid by
                # construction, no need to round-trip back through self.enum_type(...) just to
                # re-validate membership.
                value = int(value.value)
            elif isinstance(value, IntEnum):
                value = int(self.enum_type(int(value.value)))
            elif value is not None:
                self.raise_if_not_int(value)
                value = int(self.enum_type(value))
        except ValidationError:
            # Raised with the field's name already - not named a second time.
            raise
        except ValueError as error:
            # A non-member raises ValidationError, not a bare ValueError.
            validation_error = self.get_validation_error(error, value)
        if validation_error is not None:
            raise validation_error
        self.validate(value)
        return value
