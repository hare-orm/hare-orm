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
