from collections.abc import Callable
from typing import Any, ClassVar

from hare.fields import TextField


class NullableRewriteField(TextField):
    """Exercise a storage conversion that removes some values from the non-NULL selection."""

    converted_values: ClassVar[list[str]] = []

    def get_stored_value_conversion(self, old_field: Any) -> Callable[[Any], Any]:
        def convert(value: str) -> str | None:
            self.converted_values.append(value)
            return None if value.startswith("clear") else value.upper()

        return convert
