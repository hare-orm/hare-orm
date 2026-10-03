import json
import math
from collections.abc import Iterable
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_JSON_CANONICAL_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.lookups.in_list.sqlite_large_in_list import SqliteLargeInList
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function


class SqliteJsonEquality:
    """JSONField exact/__not lookups on SQLite, comparing JSON values rather than stored text -
    key order, whitespace and ``1`` vs ``1.0`` don't matter, same as Postgres jsonb equality."""

    @classmethod
    def canonicalize(cls, value: str | bytes | float | None) -> str | None:
        """Backs ``SQLITE_JSON_CANONICAL_FUNCTION_NAME``.

        Args:
            value: A JSON text (column value or bound parameter), or a number a NUMERIC-affinity
                column already converted a top-level JSON number into.

        Returns:
            The canonical JSON text, the input unchanged if it isn't valid JSON, or ``None``.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            parsed: Any = value
        else:
            try:
                parsed = json.loads(value)
            except ValueError:
                return value if isinstance(value, str) else value.decode("utf-8", "replace")
        return json.dumps(cls.normalize_numbers(parsed), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def normalize_numbers(cls, value: Any) -> Any:
        """Turns every integral float (``1.0``) into an int, recursively.

        Args:
            value: A decoded JSON value.

        Returns:
            The same structure with integral floats replaced.
        """
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: cls.normalize_numbers(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls.normalize_numbers(item) for item in value]
        return value

    @staticmethod
    def canonical_term(term: Any) -> Function:
        """Wraps ``term`` in a call to the canonicalizing UDF."""
        return Function(SQLITE_JSON_CANONICAL_FUNCTION_NAME, term)

    @classmethod
    def equal(cls, field: Term, value: Any) -> Criterion:
        """``field=value`` on a JSONField."""
        if value is None:
            return field.isnull()
        return cls.canonical_term(field) == cls.canonical_term(value)

    @classmethod
    def not_equal(cls, field: Term, value: Any) -> Criterion:
        """``field__not=value`` on a JSONField - NULL rows count as "not equal"."""
        if value is None:
            return field.notnull()
        return (cls.canonical_term(field) != cls.canonical_term(value)) | field.isnull()

    @classmethod
    def is_in(cls, field: Term, value: Any) -> Criterion:
        """``field__in=[...]`` on a JSONField."""
        if not isinstance(value, (list, tuple, set)):
            return SqliteLargeInList.is_in(field, value)
        return SqliteLargeInList.is_in(cls.canonical_term(field), cls.canonicalize_values(value))

    @classmethod
    def not_in(cls, field: Term, value: Any) -> Criterion:
        """``field__not_in=[...]`` on a JSONField - NULL rows count as "not in" unless the list holds None."""
        if not isinstance(value, (list, tuple, set)):
            return SqliteLargeInList.not_in(field, value)
        return SqliteLargeInList.not_in(cls.canonical_term(field), cls.canonicalize_values(value))

    @classmethod
    def canonicalize_values(cls, values: Iterable[Any]) -> list[str | None]:
        """Canonicalizes each encoded lookup value the way the column side is, keeping ``None``."""
        return [cls.canonicalize(value) for value in values]

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``canonicalize`` on ``connection`` as ``SQLITE_JSON_CANONICAL_FUNCTION_NAME``.

        Args:
            connection: The SQLite connection to register the function on.
        """
        native_functions = SqliteNativeFunctions.module
        canonicalize: Any = cls.canonicalize
        if native_functions is not None:
            canonicalize = native_functions.JsonCanonical(cls.canonicalize).canonicalize
        await connection.create_function(SQLITE_JSON_CANONICAL_FUNCTION_NAME, 1, canonicalize, deterministic=True)
