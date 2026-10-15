from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from hare.classes.class_path import ClassPath
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.named_schema_object import NamedSchemaObject
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.queryset import QuerySet


@dataclass(frozen=True)
class View(NamedSchemaObject):
    """A database view a model declares in ``Meta.views`` - a named ``SELECT`` in the model's schema.

    Args:
        name: The view's name.
        query: The ``SELECT`` - a queryset, a callable returning a queryset (for a queryset of the
            model declaring the view, not defined yet in its own ``Meta``), or ``RawSQLTerm`` of raw
            SQL. A queryset is written as the SQL of the connection it runs on, its values inline -
            in a migration file as ``RawSQLTerm`` too.

    Raises:
        ConfigurationError: The name is empty, or the query is neither a queryset, a callable nor a
            non-empty ``RawSQLTerm``.
    """

    query: RawSQLTerm | QuerySet[Any, Any] | Callable[[], QuerySet[Any, Any]]

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.query, RawSQLTerm):
            if not isinstance(self.query.sql, str) or not self.query.sql.strip():
                raise ConfigurationError(f"{type(self).__name__} {self.name!r}: the query can't be empty")
            return
        # Local import: the query package imports the ddl package.
        from hare.query.queryset import QuerySet

        if isinstance(self.query, str) or (not isinstance(self.query, QuerySet) and not callable(self.query)):
            raise ConfigurationError(
                f"{type(self).__name__} {self.name!r}: the query takes a queryset, a callable returning one or "
                f"RawSQLTerm(...) of raw SQL, got {self.query!r}"
            )

    def get_queryset(self) -> QuerySet[Any, Any] | None:
        """The queryset the view selects - None for raw SQL.

        Raises:
            ConfigurationError: The callable returns something else than a queryset.
        """
        if isinstance(self.query, RawSQLTerm):
            return None
        from hare.query.queryset import QuerySet

        queryset = self.query if isinstance(self.query, QuerySet) else self.query()
        if not isinstance(queryset, QuerySet):
            raise ConfigurationError(
                f"{type(self).__name__} {self.name!r}: the query callable must return a queryset, got {queryset!r}"
            )
        return queryset

    def get_query_sql(self, client: DatabaseClient | None = None) -> str:
        """The ``SELECT`` as SQL.

        Args:
            client: The connection a queryset is written for - the queryset's own when None.

        Returns:
            The SQL.
        """
        queryset = self.get_queryset()
        if queryset is None:
            return cast("RawSQLTerm", self.query).sql
        if client is not None:
            queryset = queryset.using(client)
        return queryset.sql(parameters_inline=True)

    def with_sql_query(self) -> View:
        """The view with its query as ``RawSQLTerm`` - as the migration state keeps it."""
        if isinstance(self.query, RawSQLTerm):
            return self
        return replace(self, query=RawSQLTerm(self.get_query_sql()))

    def get_options(self) -> dict[str, Any]:
        """The arguments beside the name and the query a migration file writes."""
        return {}

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        query = RawSQLTerm(self.get_query_sql())
        return ClassPath.get(type(self)), [], {"name": self.name, "query": query, **self.get_options()}
