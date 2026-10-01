from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, WithJsonSchema

from hare.contrib.request_query.constants import VALUE_SEPARATOR
from hare.contrib.request_query.types.text_parameter import TextParameter


class CommaSeparated:
    """A list written as one parameter with its items joined by commas - ``?ids=1,2,3``.
    ``CommaSeparated[int]`` is ``list[int]``; repeated parameters (``?ids=1,2&ids=3``) are joined,
    and empty items skipped.
    """

    def __class_getitem__(cls, item_type: type) -> Any:
        description = f"{item_type.__name__} values joined with commas"
        return Annotated[
            list[item_type],  # type: ignore[valid-type]
            BeforeValidator(cls.split),
            WithJsonSchema({"type": "string", "description": description}),
            TextParameter(description),
        ]

    @staticmethod
    def split(value: Any) -> Any:
        """Splits comma-joined text into its items.

        Args:
            value: The value of the parameter - text, or a list of texts for a repeated parameter.

        Returns:
            The non-empty items, stripped; ``value`` itself when it is neither text nor a list.
        """
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return value
        items: list[Any] = []
        for part in value:
            if isinstance(part, str):
                items.extend(item.strip() for item in part.split(VALUE_SEPARATOR) if item.strip())
            else:
                items.append(part)
        return items
