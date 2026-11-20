from typing import Any, ClassVar

from hare.exceptions import ValidationError
from hare.fields.field import Field


class SingleNumberField(Field[int]):
    """Accept one integer from a single-pass input to expose repeated conversion."""

    field_type = int
    SQL_TYPE = "INT"
    consumed: ClassVar[list[list[int]]] = []

    def to_db_value(self, value: Any, instance: Any) -> int | None:
        if value is None or isinstance(value, int):
            return value
        items = list(value)
        self.consumed.append(items)
        if len(items) != 1:
            raise ValidationError("Exactly one number is required")
        return items[0]
