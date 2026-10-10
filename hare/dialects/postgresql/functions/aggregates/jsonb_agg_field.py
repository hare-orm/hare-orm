from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.fields.data.json.json_field import JSONField
from hare.fields.field import Field


class JSONBAggField(JSONField[list[Any]]):
    """The output field of ``JSONBAgg`` - parses the jsonb array, then decodes each element
    through the aggregated field.

    Args:
        element_field: The aggregated field, or ``None`` to keep the parsed JSON values as-is.
    """

    def __init__(self, element_field: Field[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.element_field = element_field

    def from_db_value(self, value: Any) -> Any:
        data = super().from_db_value(value)
        if self.element_field is None or not isinstance(data, list):
            return data
        return [
            None if element is None else POSTGRESQL_DIALECT.types.get_python_value(self.element_field, element)
            for element in data
        ]
