"""Boundary ordering values checked against the selected ordering and its value types."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic_core import to_jsonable_python

from hare.contrib.request_query.enums import CursorDirection
from hare.contrib.request_query.paginations.cursor_values import CursorValues

if TYPE_CHECKING:
    from hare.fields.field import Field


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

    value_converters: ClassVar[CursorValues] = CursorValues()

    def encode(self, fields: tuple[Field[Any] | None, ...] | None = None) -> str:
        """The cursor as URL-safe text.

        Args:
            fields: Ordering field metadata for reversible values, including nested enum members.
                Omit it for cursors whose values already have a reversible JSON representation.

        Returns:
            Base64 of the cursor's JSON, without padding.
        """
        values = self.values
        if fields is not None:
            values = tuple(
                value if field is None or value is None else self.value_converters.get_db_writer(field)(value, None)
                for field, value in zip(fields, values, strict=True)
            )
        payload = {
            "direction": self.direction.value,
            "ordering": list(self.ordering),
            "values": to_jsonable_python(list(values), bytes_mode="base64"),
        }
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(text).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, text: str, value_types: tuple[Any, ...], accepts_null: tuple[bool, ...]) -> Cursor:
        """Reads a cursor made by ``encode()``.

        Args:
            text: The cursor.
            value_types: The field or Python type of each ordering value; None keeps its JSON value.
            accepts_null: Whether each ordering value can be NULL.

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
        for raw_value, value_type, value_accepts_null in zip(raw_values, value_types, accepts_null, strict=True):
            if raw_value is None and not value_accepts_null:
                raise ValueError("not a cursor of this query")
            if raw_value is None or value_type is None:
                values.append(raw_value)
                continue
            read = cls.value_converters.get_reader(value_type)
            try:
                values.append(read(raw_value))
            except (ValueError, TypeError, KeyError) as error:
                raise ValueError("not a cursor of this query") from error
        return cls(direction=direction, ordering=ordering, values=tuple(values))
