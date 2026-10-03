from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_JSON_CONTAINS_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.json.sqlite_json_keys import SqliteJsonKeys
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.sql.enums import Equality
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function


class SqliteJsonContainment:
    """Postgres jsonb containment (``@>``) as a SQLite UDF."""

    @staticmethod
    def is_same_scalar(container: Any, contained: Any) -> bool:
        """Whether two decoded JSON scalars are the same JSON value (``1`` equals ``1.0``, never ``true``)."""
        if isinstance(container, bool) or isinstance(contained, bool):
            return isinstance(container, bool) and isinstance(contained, bool) and container == contained
        if isinstance(container, (int, float)) and isinstance(contained, (int, float)):
            return container == contained
        return type(container) is type(contained) and container == contained

    @classmethod
    def contains_value(cls, container: Any, contained: Any, *, top_level: bool) -> bool:
        """Whether ``container`` contains ``contained`` - objects by key, arrays by element (order
        and repeats ignored), scalars by equality; at the top level an array also contains a
        scalar element."""
        if isinstance(container, dict):
            return isinstance(contained, dict) and all(
                key in container and cls.contains_value(container[key], item, top_level=False)
                for key, item in contained.items()
            )
        if isinstance(container, list):
            if isinstance(contained, list):
                return all(
                    any(cls.contains_value(element, item, top_level=False) for element in container)
                    for item in contained
                )
            if isinstance(contained, dict) or not top_level:
                return False
            return any(
                not isinstance(element, (list, dict)) and cls.is_same_scalar(element, contained)
                for element in container
            )
        return not isinstance(contained, (list, dict)) and cls.is_same_scalar(container, contained)

    @classmethod
    def contains(
        cls, container_json: str | bytes | int | float | None, contained_json: str | bytes | int | float | None
    ) -> int | None:
        """Backs ``SQLITE_JSON_CONTAINS_FUNCTION_NAME``.

        Args:
            container_json: JSON text, or a number a JSON path value stays as.
            contained_json: JSON text, or a number a JSON path value stays as.

        Returns:
            1 or 0, or ``None`` when either side is NULL.
        """
        if container_json is None or contained_json is None:
            return None
        container = SqliteJsonKeys.parse(container_json)
        return int(cls.contains_value(container, SqliteJsonKeys.parse(contained_json), top_level=True))

    @staticmethod
    def get_criterion(container: Term, contained: Term) -> Criterion:
        """``container @> contained``."""
        test = Function(SQLITE_JSON_CONTAINS_FUNCTION_NAME, container, contained)
        return BasicCriterion(Equality.EQ, test, ValueWrapper(1, allow_parametrize=False))

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``contains`` on ``connection`` as ``SQLITE_JSON_CONTAINS_FUNCTION_NAME``."""
        native_functions = SqliteNativeFunctions.module
        contains: Any = cls.contains
        if native_functions is not None:
            contains = native_functions.JsonContainment(
                cls.contains, SqliteJsonKeys.has_key, SqliteJsonKeys.has_keys
            ).contains
        await connection.create_function(SQLITE_JSON_CONTAINS_FUNCTION_NAME, 2, contains, deterministic=True)
