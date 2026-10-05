from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawSQLTerm:
    """Raw SQL in a declaration - a constraint's, partial index's, policy's or trigger's condition,
    an index key, an exclusion constraint's expression, a trigger's or function's body, a view's
    query, a generated column's expression, a table option - written into DDL as it is and compared
    by its text.

    Args:
        sql: The SQL.
    """

    sql: str

    def get_sql(self, context: Any = None, dialect: str | None = None) -> str:
        """The SQL, as it is."""
        return self.sql

    @classmethod
    def is_in(cls, value: Any) -> bool:
        """Whether a declaration holds raw SQL anywhere - in a collection, or in the call a migration file
        writes an object as (``deconstruct()``).

        Args:
            value: The declaration - a field, an index, the options of a model.
        """
        if isinstance(value, cls):
            return True
        if isinstance(value, dict):
            return any(cls.is_in(item) for item in value.values())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(cls.is_in(item) for item in value)
        if isinstance(value, (str, bytes, type)):
            return False
        deconstruct = getattr(value, "deconstruct", None)
        if not callable(deconstruct):
            return False
        _path, args, kwargs = deconstruct()
        return cls.is_in(args) or cls.is_in(kwargs)
