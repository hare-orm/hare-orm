"""The cursors of a cursor pagination: the ordering values of a boundary row, readable only by the
query that made them."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
from typing import Any, ClassVar, get_origin, is_typeddict

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from hare.contrib.request_query.enums import CursorDirection
from hare.contrib.request_query.paginations.constants import CURSOR_SCALAR_TYPES
from hare.core.caching.cache import Cache


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

    scalar_adapters: ClassVar[Cache[TypeAdapter[Any]]] = Cache(
        len(CURSOR_SCALAR_TYPES), holds_sql=False, keyed_by_model=False
    )

    def encode(self) -> str:
        """The cursor as URL-safe text.

        Returns:
            Base64 of the cursor's JSON, without padding.
        """
        payload = {
            "direction": self.direction.value,
            "ordering": list(self.ordering),
            "values": to_jsonable_python(list(self.values), bytes_mode="base64"),
        }
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(text).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, text: str, value_types: tuple[Any, ...], accepts_null: tuple[bool, ...]) -> Cursor:
        """Reads a cursor made by ``encode()``.

        Args:
            text: The cursor.
            value_types: The type of each ordering value, None to keep a value as JSON gives it.
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
            try:
                adapter: TypeAdapter[Any]
                if any(value_type is scalar_type for scalar_type in CURSOR_SCALAR_TYPES):
                    cached_adapter = cls.scalar_adapters.get((value_type,))
                    if cached_adapter is None:
                        cached_adapter = (
                            TypeAdapter(bytes, config=ConfigDict(val_json_bytes="base64"))
                            if value_type is bytes
                            else TypeAdapter(value_type)
                        )
                        cls.scalar_adapters[(value_type,)] = cached_adapter
                    values.append(cached_adapter.validate_python(raw_value))
                    continue
                if get_origin(value_type) in (list, dict, tuple) or is_typeddict(value_type):
                    # A wrapper carries the byte encoding into containers, including a named
                    # tuple's TypedDict, which can't take TypeAdapter's config directly.
                    adapter = TypeAdapter(
                        list.__class_getitem__(value_type), config=ConfigDict(val_json_bytes="base64")
                    )
                    values.append(adapter.validate_python([raw_value])[0])
                    continue
                adapter = TypeAdapter(value_type)
                values.append(adapter.validate_python(raw_value))
            except ValidationError as error:
                raise ValueError("not a cursor of this query") from error
        return cls(direction=direction, ordering=ordering, values=tuple(values))
