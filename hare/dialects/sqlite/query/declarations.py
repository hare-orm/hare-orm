from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.sql.builder.queries.query import Query

SqliteQuery = DeclaredSubclass.make(
    Query,
    "SqliteQuery",
    __package__,
    """A query rendered in SQLite's SQL.""",
    SQL_CONTEXT=SQLITE_DIALECT.sql_context,
)
