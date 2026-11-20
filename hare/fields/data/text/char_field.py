from __future__ import annotations

from typing import Any, Literal, TypeVar, overload

from hare.exceptions import ConfigurationError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.text.string_field import StringField
from hare.fields.data.text.text_field import TextField
from hare.fields.enums import NarrowedValueSource
from hare.fields.field import Field
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit
from hare.fields.validators.limits.max_length_validator import MaxLengthValidator

TStr = TypeVar("TStr", str, str | None)


class CharField(StringField[TStr]):
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
        if isinstance(max_length, bool) or not isinstance(max_length, int):
            raise ConfigurationError(f"'max_length' must be an int, got {max_length!r}")
        if max_length < 1:
            raise ConfigurationError("'max_length' must be >= 1")
        self.max_length = max_length
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

    # A text column reads as a str - to_python() checks only a value assigned of another type.
    keeps_native_db_values = True

    def get_assign_normalized_types(self) -> frozenset[type]:
        return self._get_unchanged_assign_types(CharField.to_python, self.field_type)
