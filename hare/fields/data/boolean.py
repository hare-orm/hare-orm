"""BooleanField."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.base.field import Field
from hare.fields.enums import NativeWriteCheck

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TBool = TypeVar("TBool", bool, bool | None)


class BooleanField(Field[TBool]):
    """
    Boolean field.
    """

    # Bool is not subclassable, so we specify type here
    field_type = bool
    SQL_TYPE = "BOOL"

    @overload
    def __init__(self: BooleanField[bool], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: BooleanField[bool | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    native_write_check = NativeWriteCheck.NOTHING

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        # bool("false") is True - a string is rejected.
        if isinstance(value, str):
            raise ValidationError(
                f"{self.model_field_name}: expected a bool, got the string {self.get_value_for_message(value)}"
            )
        return super().to_db_value(value, instance)

    def to_python(self, value: Any) -> Any:
        # The same on construction.
        if isinstance(value, str):
            raise ValidationError(
                f"{self.model_field_name}: expected a bool, got the string {self.get_value_for_message(value)}"
            )
        return super().to_python(value)
