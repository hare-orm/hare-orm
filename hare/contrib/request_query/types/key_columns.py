from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import unquote

from pydantic import BeforeValidator, WithJsonSchema

from hare.contrib.request_query.constants import VALUE_SEPARATOR
from hare.contrib.request_query.types.text_parameter import TextParameter


class KeyColumns:
    """The value of a composite primary key, or of a relation to one, written as its values joined
    with commas - ``?pk=2,1``, ``/orders/2,1/``. A value that itself holds a comma is written
    percent-encoded (``%2C``).

    ``KeyColumns[int, int]`` is ``tuple[int, int]`` read from such text; a list of keys is
    ``list[KeyColumns[int, int]]`` - one parameter per key (``?pk__in=2,1&pk__in=3,1``).
    """

    def __class_getitem__(cls, item_types: type | tuple[type, ...]) -> Any:
        item_types = item_types if isinstance(item_types, tuple) else (item_types,)
        description = (
            f"The key's values joined with commas: {', '.join(item_type.__name__ for item_type in item_types)}"
        )
        return Annotated[
            tuple[item_types],  # type: ignore[valid-type]
            BeforeValidator(cls.split),
            WithJsonSchema({"type": "string", "description": description}),
            TextParameter(description),
        ]

    @staticmethod
    def split(value: Any) -> Any:
        """Splits the text of a key into its values; any other value is left to validation.

        Args:
            value: The value of the parameter.

        Returns:
            A list of the key's values, each percent-decoded, for text; ``value`` otherwise.
        """
        if isinstance(value, str):
            return [unquote(part) for part in value.split(VALUE_SEPARATOR)]
        return value
