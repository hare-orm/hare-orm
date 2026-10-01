from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.list_parameter import ListParameter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql import SqlContext
    from hare.sql.terms.base.node import TNode


class JsonArrayValues(Term, ListParameter):
    """``(SELECT value FROM json_each(?))`` - a value list bound as ONE JSON array parameter.

    Args:
        values_json: The JSON array text.
        decode_hex: Each array element is a hex string to decode back into a BLOB.
    """

    def __init__(self, values_json: str, *, decode_hex: bool) -> None:
        super().__init__()
        self.values_json = ValueWrapper(values_json)
        self.decode_hex = decode_hex

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.values_json.nodes_()

    def get_sql(self, ctx: SqlContext) -> str:
        selected_value = "unhex(value)" if self.decode_hex else "value"
        return f"(SELECT {selected_value} FROM json_each({self.values_json.get_sql(ctx)}))"  # nosec B608

    def get_parameter_source(self) -> Term:
        return self.values_json

    def get_parameter(self, values: list[Any]) -> str | None:
        return self.get_values_json(values, self.decode_hex)

    @staticmethod
    def get_json_element(value: Any) -> int | float | str | None:
        """The JSON array element standing in for one bound parameter.

        Args:
            value: The already-encoded lookup value.

        Returns:
            The element, or None when the value has no exactly equivalent JSON form.
        """
        adapter = sqlite3.adapters.get((type(value), sqlite3.PrepareProtocol))
        if adapter is not None:
            value = adapter(value)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value if SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX else None
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            return value
        return None

    @staticmethod
    def holds_only_bytes(values: list[Any]) -> bool:
        """Whether every one of ``values`` is bytes."""
        for value in values:
            if not isinstance(value, bytes):
                return False
        return True

    @classmethod
    def get_values_json(cls, values: list[Any], decode_hex: bool) -> str | None:
        """The JSON array text standing for ``values``.

        Args:
            values: The encoded lookup values, None left out.
            decode_hex: Whether the elements are the hex text of BLOBs.

        Returns:
            The text, None when a value has no exactly equivalent element.
        """
        if decode_hex:
            if not cls.holds_only_bytes(values):
                return None
            return json.dumps([value.hex() for value in values], separators=(",", ":"))
        adapters = sqlite3.adapters
        if (int, sqlite3.PrepareProtocol) not in adapters and (str, sqlite3.PrepareProtocol) not in adapters:
            # A list of plain integers and strings is its own JSON array.
            native_functions = SqliteNativeFunctions.module
            if native_functions is not None:
                values_json = native_functions.get_json_array_text(values)
                if values_json is not None:
                    return values_json
            for value in values:
                value_type = type(value)
                if value_type is int:
                    if not SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX:
                        return None
                elif value_type is not str:
                    break
            else:
                return json.dumps(values, separators=(",", ":"))
        json_elements = []
        for value in values:
            json_element = cls.get_json_element(value)
            if json_element is None:
                return None
            json_elements.append(json_element)
        return json.dumps(json_elements, separators=(",", ":"))
