from typing import Any

from hare.exceptions import ValidationError
from hare.fields import CharField
from hare.models import Model


class TrimmedTextField(CharField):
    """Normalize assigned text before a writer that requires its canonical representation."""

    def to_python(self, value: Any) -> Any:
        value = super().to_python(value)
        return None if value is None else value.strip()

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        if isinstance(value, str) and value != value.strip():
            raise ValidationError("text must be normalized before writing")
        return super().to_db_value(value, instance)
