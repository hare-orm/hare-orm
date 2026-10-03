from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawSQLTerm:
    """Raw SQL in a schema object - a constraint's or partial index's condition, an index key, an
    exclusion constraint's expression - written into DDL as it is and compared by its text.

    Args:
        sql: The SQL.
    """

    sql: str

    def get_sql(self, context: Any = None, dialect: str | None = None) -> str:
        """The SQL, as it is."""
        return self.sql
