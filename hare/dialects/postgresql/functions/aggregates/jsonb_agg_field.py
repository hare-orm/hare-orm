from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT, POSTGRESQL_JSON_NON_FINITE_FLOAT_TEXTS
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField


class JSONBAggField(JSONField[list[Any]]):
    """The output field of ``JSONBAgg`` - parses the jsonb array, then decodes each element
    through the aggregated field.

    Args:
        element_field: The aggregated field, or ``None`` to keep the parsed JSON values as-is. A
            ``JSONField``'s element is a JSON value already - only its declared ``field_type`` is applied.
    """

    def __init__(self, element_field: Field[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.element_field = element_field

    def from_db_value(self, value: Any) -> Any:
        data = super().from_db_value(value)
        if self.element_field is None or not isinstance(data, list):
            return data
        json_field = GeneratedField.get_effective_field(self.element_field)
        if isinstance(json_field, JSONField):
            return [json_field.validate_declared_type(element) for element in data]
        return [
            None
            if element is None
            else POSTGRESQL_DIALECT.types.get_python_value(
                self.element_field, JSONBAggField.get_numbers_of_texts(self.element_field, element)
            )
            for element in data
        ]

    @staticmethod
    def get_numbers_of_texts(field: Field[Any], value: Any) -> Any:
        """An element with the non-finite floats of a float field - PostgreSQL's jsonb writes them as text
        (``"NaN"``, ``"Infinity"``) - made numbers again, in an array of floats too.

        Args:
            field: The field of the value.
            value: The JSON value.

        Returns:
            The value the field reads.
        """
        value_field = GeneratedField.get_effective_field(field)
        if isinstance(value_field, FloatField):
            return float(value) if value in POSTGRESQL_JSON_NON_FINITE_FLOAT_TEXTS else value
        if isinstance(value_field, ArrayField) and isinstance(value, list):
            return [JSONBAggField.get_numbers_of_texts(value_field.base_field, item) for item in value]
        return value
