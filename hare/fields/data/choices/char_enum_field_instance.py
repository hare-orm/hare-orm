from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ConfigurationError
from hare.fields.data.constants import AUTO_DESCRIPTION_MAX_LENGTH
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit

if TYPE_CHECKING:
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model


class CharEnumFieldInstance(CharField[str]):
    """The field ``CharEnumField()`` makes: a member of an enum, stored as its value's text in a
    ``VARCHAR`` as long as the longest value - read back as the member."""

    #: The enum whose members the field holds.
    enum_type: type[Enum]
    #: A migration declares the enum itself, its members as they are when it is written - the column's
    #: enum type or its CHECK holds them, and a member added to the class later is a change.
    snapshots_enum_in_migrations: ClassVar[bool] = True
    #: Whether the column is of an enum type of its own on every database - holding the members, it takes
    #: no CHECK of them.
    has_native_enum_type: ClassVar[bool] = False

    def __init__(
        self,
        enum_type: type[Enum],
        description: str | None = None,
        max_length: int = 0,
        **kwargs: Any,
    ) -> None:
        if not (isinstance(enum_type, type) and issubclass(enum_type, Enum)):
            raise ConfigurationError(f"CharEnumField takes an enum class, got {enum_type!r}")
        # The column's description lists the members, unless one is given.
        if description is None:
            description = "\n".join([f"{member.name}: {str(member.value)}" for member in enum_type])[
                :AUTO_DESCRIPTION_MAX_LENGTH
            ]

        longest_value_length = max((len(str(item.value)) for item in enum_type), default=0)
        if max_length == 0:
            max_length = longest_value_length
        elif isinstance(max_length, int) and max_length < longest_value_length:
            raise ConfigurationError(
                f"CharEnumField: max_length={max_length} is shorter than the longest value of "
                f"{enum_type.__name__} ({longest_value_length} characters)"
            )

        super().__init__(description=description, max_length=max_length, **kwargs)
        self.enum_type = enum_type
        default = kwargs.get("default")
        if default is not None and not callable(default):
            try:
                self.get_member(default)
            except ValueError:
                raise ConfigurationError(
                    f"CharEnumField: default {default!r} is no member or value of {enum_type.__name__}"
                ) from None

    def get_member(self, value: Any) -> Enum:
        """Finds this field's enum member for a member, a foreign enum member, a raw value or its
        stored text - a non-string enum (e.g. int values) is stored as ``str(member.value)``.

        Raises:
            ValueError: No member matches.
        """
        if isinstance(value, self.enum_type):
            return value
        if isinstance(value, Enum):
            value = value.value
        try:
            return self.enum_type(value)
        except ValueError:
            if isinstance(value, str):
                for member in self.enum_type:
                    if str(member.value) == value:
                        return member
            raise

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        """An enum losing a member another field held, or a text column becoming the enum's, stores only its
        own members; else what a shorter text needs."""
        if isinstance(old_field, CharEnumFieldInstance):
            limit = EnumValuesLimit.get_for_dropped_members(self.enum_type, old_field.enum_type, str)
            if limit is not None:
                return limit
        elif isinstance(old_field, (CharField, TextField)):
            # A text of no member would be read as none.
            return EnumValuesLimit(frozenset(self.get_stored_member_values()))
        return super().get_narrowing_limit(old_field)

    def get_assign_normalized_types(self) -> frozenset[type]:
        return frozenset({self.enum_type})

    def to_python(self, value: str | None) -> Enum | None:
        if value is None:
            return None
        try:
            return self.get_member(value)
        except ValueError as error:
            validation_error = self.get_validation_error(error, value)
        raise validation_error

    def get_stored_member_values(self) -> tuple[str, ...]:
        """The texts the members are stored as."""
        return tuple(str(member.value) for member in self.enum_type)

    def to_db_value(self, value: Enum | None | str, instance: type[Model] | Model | None) -> str | None:
        if value is None:
            self.validate(value)
            return None
        validation_error = None
        try:
            # A member of some OTHER Enum class isn't valid by construction - it has to map onto
            # one of this field's own members, not just fit the column length.
            db_value = str(self.get_member(value).value)
        except ValueError as error:
            validation_error = self.get_validation_error(error, value)
        if validation_error is not None:
            raise validation_error
        self.validate(db_value)
        return db_value
