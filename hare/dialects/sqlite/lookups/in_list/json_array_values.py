from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.parameters.sqlite_encoded_text import SqliteEncodedText
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.sql.terms.parameters.list_parameter import ListParameter
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.sql import SqlContext
    from hare.sql.terms.node import Node


class JsonArrayValues(Term, ListParameter):
    """``(SELECT value FROM json_each(?))`` - a value list bound as ONE JSON array parameter.

    Args:
        values_json: The JSON array text.
        decode_hex: Each array element is a hex string to decode back into a BLOB.
    """

    def __init__(self, values_json: str, *, decode_hex: bool, adapters: SqliteParameterAdapters | None = None) -> None:
        super().__init__()
        self.values_json = ValueWrapper(SqliteEncodedText(values_json))
        self.adapters = adapters or SqliteParameterAdapters()
        self.decode_hex = decode_hex

    def get_children(self) -> Iterator[Node]:
        yield self.values_json

    def get_sql(self, sql_context: SqlContext) -> str:
        selected_value = "unhex(value)" if self.decode_hex else "value"
        return f"(SELECT {selected_value} FROM json_each({self.values_json.get_sql(sql_context)}))"  # nosec B608

    def get_parameter_source(self) -> Term:
        return self.values_json

    def get_parameter_encoder(self) -> Callable[[list[Any]], SqliteEncodedText | None]:
        return partial(type(self).get_adapted_values_json, decode_hex=self.decode_hex, adapters=self.adapters)

    @staticmethod
    def get_json_element(value: Any) -> int | float | str | None:
        """The JSON array element standing in for one bound parameter.

        Args:
            value: The already-encoded lookup value.

        Returns:
            The element, or None when the value has no exactly equivalent JSON form.
        """
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
        return all(isinstance(value, bytes) for value in values)

    @classmethod
    def get_adapted_values_json(
        cls, values: list[Any], decode_hex: bool, adapters: SqliteParameterAdapters
    ) -> SqliteEncodedText | None:
        """Adapt fresh list elements using the snapshot that prepared the SQL form."""
        return cls.get_values_json([adapters.adapt(value) for value in values], decode_hex)

    @classmethod
    def get_values_json(cls, values: list[Any], decode_hex: bool) -> SqliteEncodedText | None:
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
            return SqliteEncodedText(json.dumps([value.hex() for value in values], separators=(",", ":")))
        # Values are already adapted; the native helper performs JSON encoding only.
        native_functions = SqliteNativeFunctions.module
        if native_functions is not None:
            values_json = native_functions.get_json_array_text(values)
            if values_json is not None:
                return SqliteEncodedText(values_json)
        json_elements = []
        for value in values:
            json_element = cls.get_json_element(value)
            if json_element is None:
                return None
            json_elements.append(json_element)
        return SqliteEncodedText(json.dumps(json_elements, separators=(",", ":")))
