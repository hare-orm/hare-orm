from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.base.field import Field
from hare.fields.constants import NULL_BYTE_MESSAGE
from hare.fields.data.boolean import BooleanField
from hare.fields.enums import NarrowedValueSource, NativeWriteCheck
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit
from hare.fields.validators.max_length_validator import MaxLengthValidator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.data.text.text_field import TextField

TStr = TypeVar("TStr", str, str | None)


class CharField(Field[TStr]):
    """A character field.

    Args:
        max_length: The most characters the value may have.
    """

    field_type = str

    @overload
    def __init__(self: CharField[str], max_length: int, *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: CharField[str | None], max_length: int, *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, max_length: int, **kwargs: Any) -> None:
        if int(max_length) < 1:
            raise ConfigurationError("'max_length' must be >= 1")
        self.max_length = int(max_length)
        super().__init__(**kwargs)
        self.validators.append(MaxLengthValidator(self.max_length))

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "max_length": self.max_length,
        }

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"VARCHAR({self.max_length})"

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        if isinstance(old_field, CharField) and self.max_length >= old_field.max_length:
            return None
        # Any other column - text of any length, a number, a UUID - is checked by the text it
        # turns into.
        if isinstance(old_field, (CharField, TextField)):
            source = NarrowedValueSource.TEXT
        elif isinstance(old_field, BooleanField):
            source = NarrowedValueSource.BOOLEAN
        else:
            source = NarrowedValueSource.OTHER
        return TextLengthLimit(self.max_length, source)

    native_write_check = NativeWriteCheck.NO_NULL_BYTE

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        # Postgres can't store a null byte in text - refused on every backend.
        if isinstance(value, str) and "\x00" in value:
            raise ValidationError(f"{self.model_field_name}: {NULL_BYTE_MESSAGE}")
        return value
