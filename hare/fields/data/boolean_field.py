from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.enums import NativeWriteCheck
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.models import Model

TBool = TypeVar("TBool", bool, bool | None)


class BooleanField(Field[TBool]):
    """A boolean - a ``BOOL`` column, or an integer one where the database stores booleans as integers.
    Takes only ``True`` and ``False``."""

    field_type = bool
    SQL_TYPE = "BOOL"

    @overload
    def __init__(self: BooleanField[bool], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: BooleanField[bool | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    native_write_check = NativeWriteCheck.NOTHING

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        if value is not None and type(value) is not bool:
            self.raise_if_not_boolean(value)
        return super().to_db_value(value, instance)

    def to_python(self, value: Any) -> Any:
        if value is not None and type(value) is not bool:
            self.raise_if_not_boolean(value)
        return super().to_python(value)

    def from_db_value(self, value: Any) -> Any:
        # A database storing a boolean as an integer reads any integer it holds - an EXISTS too.
        return None if value is None else bool(value)

    def raise_if_not_boolean(self, value: Any) -> None:
        """Refuses a value that is no bool - bool("false") and bool([0]) are True, and a 0/1 is a number
        given for a flag. A database storing booleans as integers is read by ``from_db_value()``.

        Args:
            value: A value that isn't a bool.

        Raises:
            ValidationError: Always.
        """
        raise ValidationError(f"{self.model_field_name}: expected a bool, got {type(value).__name__}")
