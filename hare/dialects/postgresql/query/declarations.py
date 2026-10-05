from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.sql.builder.queries.query import Query

PostgresqlQuery = DeclaredSubclass.make(
    Query,
    "PostgresqlQuery",
    __package__,
    """A query rendered in PostgreSQL's SQL.""",
    SQL_CONTEXT=POSTGRESQL_DIALECT.sql_context,
)
