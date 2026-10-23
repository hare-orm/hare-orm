from __future__ import annotations

from typing import Any

from hare.exceptions import FieldError
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.sql.enums import DateTruncSource


class DateFunctionSource:
    """The type of date/time value a date function's argument holds."""

    @staticmethod
    def get(function_name: str, output_field: Field[Any] | None) -> DateTruncSource:
        """The type of value an argument holds.

        Args:
            function_name: The function's name, for the message.
            output_field: The argument's field.

        Returns:
            ``datetime``, ``naive datetime``, ``date`` or ``time``.

        Raises:
            FieldError: The argument isn't a date, time or datetime.
        """
        effective_field = GeneratedField.get_effective_field(output_field) if output_field is not None else None
        if effective_field is not None and effective_field.date_function_source is not None:
            return effective_field.date_function_source
        raise FieldError(f"{function_name}() needs a DateField, DatetimeField or TimeField value")
