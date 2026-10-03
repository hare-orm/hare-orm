from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.fields.constants import (
    AUTO_DESCRIPTION_MAX_LENGTH,
)
from hare.fields.data.text.char_field import CharField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class CharEnumFieldInstance(CharField[str]):
    enum_type: type[Enum]

    def __init__(
        self,
        enum_type: type[Enum],
        description: str | None = None,
        max_length: int = 0,
        **kwargs: Any,
    ) -> None:
        # Automatic description for the field if not specified by the user
        if description is None:
            description = "\n".join([f"{e.name}: {str(e.value)}" for e in enum_type])[:AUTO_DESCRIPTION_MAX_LENGTH]

        # Automatic CharField max_length
        if max_length == 0:
            for item in enum_type:
                item_len = len(str(item.value))
                if item_len > max_length:
                    max_length = item_len

        super().__init__(description=description, max_length=max_length, **kwargs)
        self.enum_type = enum_type

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

    def get_assign_normalized_types(self) -> frozenset[type]:
        return frozenset({self.enum_type})

    def to_python(self, value: str | None) -> Enum | None:
        if value is None:
            return None
        try:
            return self.get_member(value)
        except ValueError as exc:
            validation_error = self.get_validation_error(exc, value)
        raise validation_error

    def to_db_value(self, value: Enum | None | str, instance: type[Model] | Model) -> str | None:
        if value is None:
            self.validate(value)
            return None
        validation_error = None
        try:
            # A member of some OTHER Enum class isn't valid by construction - it has to map onto
            # one of this field's own members, not just fit the column length.
            db_value = str(self.get_member(value).value)
        except ValueError as exc:
            validation_error = self.get_validation_error(exc, value)
        if validation_error is not None:
            raise validation_error
        self.validate(db_value)
        return db_value
