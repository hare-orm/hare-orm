from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from hare.dialects.base.features import Features
from hare.dialects.base.parameters.large_in_list import LargeInList
from hare.dialects.sqlite.lookups.in_list.json_array_row_values import JsonArrayRowValues
from hare.dialects.sqlite.lookups.in_list.json_array_values import JsonArrayValues
from hare.dialects.sqlite.parameters.constants import SQLITE_IN_JSON_ARRAY_THRESHOLD
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term


class SqliteLargeInList(LargeInList):
    """``__in``/``__not_in`` on SQLite for a value list too long to bind one parameter per value.

    The list is bound as one JSON array only when every value converts to a JSON element that
    compares exactly like the bound parameter would have; any other list keeps the plain
    ``IN (?, ?, ...)`` form.
    """

    @staticmethod
    def get_sqlite_client_features() -> Features:
        """The features of the SQLite driver of ``sqlite+aiosqlite://`` - its library is the server of every
        connection it opens, so whether it has ``unhex()`` is the same for each."""
        # Local imports: the registry and the dialect's constants import the dialect, which imports this.
        from hare.dialects.dialect_registry import DialectRegistry
        from hare.dialects.sqlite.constants import SQLITE_AIOSQLITE_DRIVER_NAME

        return DialectRegistry.get_driver(SQLITE_AIOSQLITE_DRIVER_NAME).get_client_classes()[0].features

    @classmethod
    def get_container(cls, non_null_values: list[Any]) -> JsonArrayValues | None:
        """The one-parameter JSON array container for a long value list.

        Args:
            non_null_values: The lookup values, None already stripped.

        Returns:
            The container, or None when the list is short or has a value JSON can't carry exactly.
        """
        if len(non_null_values) < SQLITE_IN_JSON_ARRAY_THRESHOLD:
            return None
        decode_hex = JsonArrayValues.holds_only_bytes(non_null_values)
        if decode_hex and not cls.get_sqlite_client_features().supports_unhex:
            return None
        values_json = JsonArrayValues.get_values_json(non_null_values, decode_hex)
        return None if values_json is None else JsonArrayValues(values_json, decode_hex=decode_hex)

    @classmethod
    def get_membership_criterion(
        cls, term: Term, values: Sequence[Any], non_null_values: list[Any], element_type: str | None
    ) -> Criterion | None:
        container = cls.get_container(non_null_values)
        return None if container is None else term.isin(container)

    @classmethod
    def get_row_container(
        cls, value_rows: list[tuple[Any, ...]], element_types: Sequence[str | None] | None = None
    ) -> JsonArrayRowValues | None:
        """The one-parameter JSON array-of-arrays container for a long list of value rows.

        Args:
            value_rows: The value rows, each holding one value per compared column.
            element_types: Unused - each JSON value carries its own type.

        Returns:
            The container, or None when the rows bind few parameters or hold a value JSON can't
            carry exactly.
        """
        if not value_rows or len(value_rows) * len(value_rows[0]) < SQLITE_IN_JSON_ARRAY_THRESHOLD:
            return None
        column_count = len(value_rows[0])
        decode_hex_columns = tuple(
            all(isinstance(row[index], bytes) for row in value_rows) for index in range(column_count)
        )
        if any(decode_hex_columns) and not cls.get_sqlite_client_features().supports_unhex:
            return None
        json_rows = []
        for row in value_rows:
            json_row: list[int | float | str | None] = []
            for value, decode_hex in zip(row, decode_hex_columns, strict=True):
                if decode_hex:
                    json_row.append(value.hex())
                    continue
                if value is None:
                    json_row.append(None)
                    continue
                json_element = JsonArrayValues.get_json_element(value)
                if json_element is None:
                    return None
                json_row.append(json_element)
            json_rows.append(json_row)
        return JsonArrayRowValues(json.dumps(json_rows, separators=(",", ":")), decode_hex_columns)
