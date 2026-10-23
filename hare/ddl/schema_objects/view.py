from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from hare.classes.class_path import ClassPath
from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.named_schema_object import NamedSchemaObject
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from typing import Self

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.query.queryset.model_query import ModelQuery


@dataclass(frozen=True)
class View(NamedSchemaObject):
    """A database view a model declares in ``Meta.views`` - a named ``SELECT`` in the model's schema.

    Args:
        name: The view's name.
        query: The ``SELECT`` - a queryset, ``ModelQuery()`` for a queryset of the model declaring the
            view (not built yet in its own ``Meta``), or ``RawSQLTerm`` of raw SQL. A queryset is
            written as the SQL of the connection it runs on, its values inline - in a migration file as
            ``RawSQLTerm`` too.
        read_columns: The columns the query reads, ``(table, column)`` each - a migration changing one
            drops the view before and creates it again after. Taken from a queryset itself; for raw SQL,
            the ones you name.

    Raises:
        ConfigurationError: The name is empty, the query is neither a queryset, ``ModelQuery()`` nor a
            non-empty ``RawSQLTerm``, or ``read_columns`` isn't pairs of names.
    """

    query: RawSQLTerm | QuerySet[Any, Any] | ModelQuery
    read_columns: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(cast("object", self.read_columns), str) or not all(
            isinstance(column, (tuple, list))
            and len(column) == 2
            and all(isinstance(name, str) and name for name in column)
            for column in self.read_columns
        ):
            raise ConfigurationError(
                f"{type(self).__name__} {self.name!r}: read_columns takes (table, column) pairs, "
                f"got {self.read_columns!r}"
            )
        object.__setattr__(self, "read_columns", tuple(sorted({tuple(column) for column in self.read_columns})))
        if isinstance(self.query, RawSQLTerm):
            if not isinstance(self.query.sql, str) or not self.query.sql.strip():
                raise ConfigurationError(f"{type(self).__name__} {self.name!r}: the query can't be empty")
            return
        # Local import: the query package imports the ddl package.
        from hare.query.queryset import QuerySet
        from hare.query.queryset.model_query import ModelQuery

        if not isinstance(self.query, (QuerySet, ModelQuery)):
            raise ConfigurationError(
                f"{type(self).__name__} {self.name!r}: the query takes a queryset, ModelQuery() or "
                f"RawSQLTerm(...) of raw SQL, got {ValueDescriptions.get_shown_value(self.query)}"
            )

    def for_model(self, model: type[Model]) -> Self:
        """The view as the model declaring it has it - a ``ModelQuery()`` made on that model.

        Args:
            model: The model.
        """
        from hare.query.queryset.model_query import ModelQuery

        if not isinstance(self.query, ModelQuery):
            return self
        return replace(self, query=self.query.for_model(model))

    def get_queryset(self) -> QuerySet[Any, Any] | None:
        """The queryset the view selects - None for raw SQL.

        Raises:
            ConfigurationError: A ``ModelQuery()`` isn't made on a model's queryset.
        """
        if isinstance(self.query, RawSQLTerm):
            return None
        from hare.query.queryset.model_query import ModelQuery

        if isinstance(self.query, ModelQuery):
            return self.query.get_queryset()
        return self.query

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

    def get_read_columns(self) -> tuple[tuple[str, str], ...]:
        """The columns the query reads - a queryset's own, else ``read_columns``."""
        queryset = self.get_queryset()
        if queryset is None:
            return self.read_columns
        return tuple(sorted(queryset.get_read_columns()))

    def with_sql_query(self) -> View:
        """The view with its query as ``RawSQLTerm`` - as the migration state keeps it."""
        if isinstance(self.query, RawSQLTerm):
            return self
        return replace(self, query=RawSQLTerm(self.get_query_sql()), read_columns=self.get_read_columns())

    def get_options(self) -> dict[str, Any]:
        """The arguments beside the name and the query a migration file writes."""
        read_columns = self.get_read_columns()
        return {"read_columns": read_columns} if read_columns else {}

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        query = RawSQLTerm(self.get_query_sql())
        return ClassPath.get(type(self)), [], {"name": self.name, "query": query, **self.get_options()}
