import json
from decimal import Decimal
from typing import Any, ClassVar

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_JSON_COMPARE_FUNCTION_NAME,
    SQLITE_JSON_SORT_KEY_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.sort_keys import SqliteSortKeys
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function


class SqliteJsonOrdering:
    """Orders JSON path values as Postgres orders ``jsonb``: object > array > boolean > number >
    string > null, a container with more members greater; at the top level an empty array sorts
    below null and a scalar below an array of one element."""

    #: The rank of each type of JSON value.
    TYPE_RANKS: ClassVar[dict[type, int]] = {type(None): 0, str: 1, Decimal: 2, bool: 3, list: 4, dict: 5}

    @staticmethod
    def get_json_value(value: Any) -> Any:
        """A JSON path's SQLite value as a JSON value - a number as-is, anything else JSON text."""
        if isinstance(value, (int, float)):
            return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return json.loads(value, parse_float=Decimal, parse_int=Decimal)

    @staticmethod
    def get_sign(difference: Any) -> int:
        """-1, 0 or 1 by the sign of the difference."""
        return (difference > 0) - (difference < 0)

    @staticmethod
    def get_key_order(key: str) -> tuple[int, bytes]:
        """The order jsonb stores an object's keys in - shorter first, then by bytes."""
        encoded = key.encode()
        return len(encoded), encoded

    @classmethod
    def compare_values(cls, left: Any, right: Any) -> int:
        """Compares two JSON values below the top level."""
        left_rank, right_rank = cls.TYPE_RANKS[type(left)], cls.TYPE_RANKS[type(right)]
        if left_rank != right_rank:
            return cls.get_sign(left_rank - right_rank)
        if isinstance(left, list):
            if len(left) != len(right):
                return cls.get_sign(len(left) - len(right))
            for left_element, right_element in zip(left, right, strict=True):
                if result := cls.compare_values(left_element, right_element):
                    return result
            return 0
        if isinstance(left, dict):
            if len(left) != len(right):
                return cls.get_sign(len(left) - len(right))
            left_keys = sorted(left, key=cls.get_key_order)
            right_keys = sorted(right, key=cls.get_key_order)
            for left_key, right_key in zip(left_keys, right_keys, strict=True):
                if result := cls.compare_values(left_key, right_key) or cls.compare_values(
                    left[left_key], right[right_key]
                ):
                    return result
            return 0
        if left is None:
            return 0
        return (left > right) - (left < right)

    @classmethod
    def compare(cls, left: Any, right: Any) -> int | None:
        """Backs ``SQLITE_JSON_COMPARE_FUNCTION_NAME``: compares two top-level JSON values.

        Args:
            left: The JSON path's value - a number, or JSON text.
            right: The compared value, in the same form.

        Returns:
            -1, 0 or 1, or ``None`` when either is NULL.
        """
        if left is None or right is None:
            return None
        left_value, right_value = cls.get_json_value(left), cls.get_json_value(right)
        if not isinstance(left_value, dict) and not isinstance(right_value, dict):
            # A top-level scalar is held as an array of one element, compared by size first.
            left_size = len(left_value) if isinstance(left_value, list) else 1
            right_size = len(right_value) if isinstance(right_value, list) else 1
            if left_size != right_size:
                return cls.get_sign(left_size - right_size)
            if isinstance(left_value, list) != isinstance(right_value, list):
                return 1 if isinstance(left_value, list) else -1
        return cls.compare_values(left_value, right_value)

    @classmethod
    def get_value_key(cls, value: Any) -> bytes:
        """The byte key of a JSON value below the top level, in ``compare_values()``'s order - no key is
        the start of another."""
        rank = cls.TYPE_RANKS[type(value)]
        if value is None:
            return b"\x00"
        if isinstance(value, str):
            return b"\x01" + SqliteSortKeys.get_terminated_text(value)
        if isinstance(value, Decimal):
            return b"\x02" + SqliteSortKeys.get_number_key(value)
        if isinstance(value, bool):
            return b"\x03" + (b"\x01" if value else b"\x00")
        if isinstance(value, list):
            return b"\x04" + len(value).to_bytes(8, "big") + b"".join(map(cls.get_value_key, value))
        members = b"".join(
            b"\x01" + SqliteSortKeys.get_terminated_text(key) + cls.get_value_key(value[key])
            for key in sorted(value, key=cls.get_key_order)
        )
        return rank.to_bytes(1, "big") + len(value).to_bytes(8, "big") + members

    @classmethod
    def get_sort_key_bytes(cls, value: Any) -> bytes | None:
        """Backs ``SQLITE_JSON_SORT_KEY_FUNCTION_NAME``: the byte key ordering a top-level JSON value
        as ``compare()`` does.

        Args:
            value: The JSON path's value - a number, or JSON text.

        Returns:
            The key; None for NULL.
        """
        if value is None:
            return None
        json_value = cls.get_json_value(value)
        if isinstance(json_value, dict):
            return b"\x02" + cls.get_value_key(json_value)
        is_list = isinstance(json_value, list)
        size = len(json_value) if is_list else 1
        return b"\x01" + size.to_bytes(8, "big") + (b"\x01" if is_list else b"\x00") + cls.get_value_key(json_value)

    @staticmethod
    def get_comparison(field: Term, value: Any) -> Term:
        """The comparison of a JSON path's value with a value, as -1, 0 or 1."""
        return Function(SQLITE_JSON_COMPARE_FUNCTION_NAME, field, value)

    @classmethod
    def greater_than(cls, field: Term, value: Any) -> Criterion:
        return cls.get_comparison(field, value) > 0

    @classmethod
    def greater_equal(cls, field: Term, value: Any) -> Criterion:
        return cls.get_comparison(field, value) >= 0

    @classmethod
    def less_than(cls, field: Term, value: Any) -> Criterion:
        return cls.get_comparison(field, value) < 0

    @classmethod
    def less_equal(cls, field: Term, value: Any) -> Criterion:
        return cls.get_comparison(field, value) <= 0

    @classmethod
    def between(cls, field: Term, value: list[Any]) -> Criterion:
        lower, upper = value
        return (cls.get_comparison(field, lower) >= 0) & (cls.get_comparison(field, upper) <= 0)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``compare`` and the ``jsonb`` order sort key on a SQLite connection."""
        native_functions = SqliteNativeFunctions.module
        compare: Any = cls.compare
        sort_key: Any = cls.get_sort_key_bytes
        if native_functions is not None:
            order = native_functions.JsonOrder(cls.get_sort_key_bytes, cls.compare)
            compare, sort_key = order.compare, order.sort_key
        await connection.create_function(SQLITE_JSON_COMPARE_FUNCTION_NAME, 2, compare, deterministic=True)
        await connection.create_function(SQLITE_JSON_SORT_KEY_FUNCTION_NAME, 1, sort_key, deterministic=True)
