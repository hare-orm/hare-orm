"""The cursors of a cursor pagination: the ordering values of a boundary row, readable only by the
query that made them."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from hare.contrib.request_query.enums import CursorDirection


@dataclasses.dataclass(frozen=True, slots=True)
class Cursor:
    """Where a page of a cursor pagination starts.

    Attributes:
        direction: ``NEXT`` - the rows after ``values``; ``PREVIOUS`` - the rows before them.
        ordering: The ordering the values belong to - a cursor of another ordering is refused.
        values: The ordering values of the boundary row, one per ordering column.
    """

    direction: CursorDirection
    ordering: tuple[str, ...]
    values: tuple[Any, ...]

    def encode(self) -> str:
        """The cursor as URL-safe text.

        Returns:
            Base64 of the cursor's JSON, without padding.
        """
        payload = {
            "direction": self.direction.value,
            "ordering": list(self.ordering),
            "values": to_jsonable_python(list(self.values)),
        }
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(text).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, text: str, value_types: tuple[Any, ...]) -> Cursor:
        """Reads a cursor made by ``encode()``.

        Args:
            text: The cursor.
            value_types: The type of each ordering value, None to keep a value as JSON gives it.

        Returns:
            The cursor, its values converted to their types.

        Raises:
            ValueError: The text isn't a cursor, or its values don't fit the types.
        """
        try:
            payload = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8"))
            direction = CursorDirection(payload["direction"])
            ordering = tuple(payload["ordering"])
            raw_values = payload["values"]
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("not a cursor of this query") from error
        if not isinstance(raw_values, list) or len(raw_values) != len(value_types):
            raise ValueError("not a cursor of this query")
        if not all(isinstance(name, str) for name in ordering):
            raise ValueError("not a cursor of this query")
        values = []
        for raw_value, value_type in zip(raw_values, value_types, strict=True):
            if raw_value is None or value_type is None:
                values.append(raw_value)
                continue
            try:
                values.append(TypeAdapter(value_type).validate_python(raw_value))
            except ValidationError as error:
                raise ValueError("not a cursor of this query") from error
        return cls(direction=direction, ordering=ordering, values=tuple(values))
