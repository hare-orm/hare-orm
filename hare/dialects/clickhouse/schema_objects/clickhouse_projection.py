from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from hare.classes.class_path import ClassPath
from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_PROJECTION_ORDERS
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from typing import Self

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.query.queryset.model_query import ModelQuery


@dataclasses.dataclass(frozen=True)
class ClickhouseProjection:
    """A projection of a ClickHouse table - its rows kept a second time inside each part, sorted or
    aggregated as a query of the table says, which the server reads in place of the table where a query
    fits it::

        ClickhouseTableOptions(
            projections=(ClickhouseProjection("by_site", ModelQuery().values("site").annotate(views=Count("id"))),)
        )

    Attributes:
        name: The projection's name.
        query: Its ``SELECT`` - a queryset of the table's model or ``ModelQuery()`` of it, written without
            ``FROM`` in ClickHouse's SQL, its columns known to hare; or ``RawSQLTerm`` of a ``SELECT``
            without ``FROM``, with ``GROUP BY`` or ``ORDER BY``, written as it is. A queryset selects and
            groups and orders - no filter, join or limit: a projection keeps every row.
        read_columns: The columns of the table the query reads - taken from a queryset itself; for raw SQL,
            the ones you name: a migration changing one drops the projection before and adds it after.

    Raises:
        ConfigurationError: ``name`` is empty, ``query`` is neither a queryset, ``ModelQuery()`` nor
            ``RawSQLTerm`` of a ``SELECT``, or ``read_columns`` isn't names.
    """

    name: str
    query: RawSQLTerm | QuerySet[Any, Any] | ModelQuery
    read_columns: tuple[str, ...] = dataclasses.field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigurationError(f"ClickhouseProjection.name must be a non-empty string, got {self.name!r}")
        if isinstance(cast("object", self.read_columns), str) or not all(
            isinstance(column, str) and column for column in self.read_columns
        ):
            raise ConfigurationError(
                f"ClickhouseProjection {self.name!r}: read_columns takes column names, got {self.read_columns!r}"
            )
        object.__setattr__(self, "read_columns", tuple(sorted(set(self.read_columns))))
        if isinstance(self.query, RawSQLTerm):
            if not self.query.sql.strip().upper().startswith("SELECT"):
                raise ConfigurationError(
                    f"ClickhouseProjection {self.name!r}: the query takes RawSQLTerm(...) of a SELECT of the table, "
                    f"got {ValueDescriptions.get_shown_value(self.query)}"
                )
            return
        # Local import: the query package imports the ddl and dialects packages.
        from hare.query.queryset import QuerySet
        from hare.query.queryset.model_query import ModelQuery

        if not isinstance(self.query, (QuerySet, ModelQuery)):
            raise ConfigurationError(
                f"ClickhouseProjection {self.name!r}: the query takes a queryset of the table's model, "
                f"ModelQuery() or RawSQLTerm(...) of a SELECT, got {ValueDescriptions.get_shown_value(self.query)}"
            )

    def for_model(self, model: type[Model]) -> Self:
        """The projection as the model declaring it has it - a ``ModelQuery()`` made on that model.

        Args:
            model: The model.
        """
        from hare.query.queryset.model_query import ModelQuery

        if not isinstance(self.query, ModelQuery):
            return self
        return dataclasses.replace(self, query=self.query.for_model(model))

    def get_queryset(self) -> QuerySet[Any, Any] | None:
        """The queryset the projection selects - None for raw SQL."""
        if isinstance(self.query, RawSQLTerm):
            return None
        from hare.query.queryset.model_query import ModelQuery

        if isinstance(self.query, ModelQuery):
            return self.query.get_queryset()
        return self.query

    def raise_if_not_of(self, model: type[Model], client: DatabaseClient | None = None) -> None:
        """Rejects a queryset of another model than the table's, or one keeping some rows only.

        Args:
            model: The model of the table.
            client: The connection the query is built for - the queryset's own when None.

        Raises:
            ConfigurationError: The queryset is of another model, or filters, joins or limits its rows.
        """
        queryset = self.get_queryset()
        if queryset is None:
            return
        if queryset.model is not model:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseProjection {self.name!r} is a queryset of {queryset.model.__name__} - "
                "a projection is a query of its own table"
            )
        builder = self.get_query_builder(queryset, client)
        if builder._wheres or builder._joins or builder._limit is not None or builder._offset is not None:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseProjection {self.name!r} filters, joins or limits its rows - a "
                "projection keeps every row of the table (a model's default filter, a soft delete's too: take a "
                "queryset without it)"
            )
        if any(order not in CLICKHOUSE_PROJECTION_ORDERS for _term, order in builder._orderbys):
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseProjection {self.name!r} orders its rows otherwise than ascending - "
                "ClickHouse sorts a projection ascending only, NULL last"
            )

    @staticmethod
    def get_query_builder(queryset: QuerySet[Any, Any], client: DatabaseClient | None = None) -> Any:
        """The query a queryset builds, for its connection or another one.

        Args:
            queryset: The queryset.
            client: The connection - the queryset's own when None.

        Returns:
            The query builder.
        """
        if client is not None:
            queryset = queryset.using(client)
        execution_query = queryset._get_compiler()._get_execution_query()
        execution_query._make_query()
        return execution_query.query

    def get_query_sql(self, client: DatabaseClient | None = None) -> str:
        """The ``SELECT`` as the projection declares it - without ``FROM``, values inline.

        Args:
            client: The connection a queryset is written for - the queryset's own when None.

        Returns:
            The SQL.
        """
        queryset = self.get_queryset()
        if queryset is None:
            return cast("RawSQLTerm", self.query).sql.strip()
        builder = self.get_query_builder(queryset, client).without_from()
        # A projection's ORDER BY is a sorting key - ascending, written without a direction.
        builder._orderbys = [(term, None) for term, _order in builder._orderbys]
        return str(builder.get_sql())

    def get_read_columns(self) -> tuple[str, ...]:
        """The columns of the table the query reads - a queryset's own, else ``read_columns``."""
        queryset = self.get_queryset()
        if queryset is None:
            return self.read_columns
        return tuple(sorted({column for _table, column in queryset.get_read_columns()}))

    def with_sql_query(self, model: type[Model]) -> ClickhouseProjection:
        """The projection with its query as ``RawSQLTerm`` - as a migration file keeps it.

        Args:
            model: The model declaring it - a ``ModelQuery()`` is made on it.

        Raises:
            ConfigurationError: The queryset is of another model, or filters, joins or limits its rows.
        """
        if isinstance(self.query, RawSQLTerm):
            return self
        projection = self.for_model(model)
        projection.raise_if_not_of(model)
        return dataclasses.replace(
            projection, query=RawSQLTerm(projection.get_query_sql()), read_columns=projection.get_read_columns()
        )

    def get_definition_sql(self, model: type[Model], editor: BaseSchemaEditor) -> str:
        """The projection as ``ADD PROJECTION`` declares it.

        Args:
            model: The model of the table.
            editor: The schema editor of the connection the DDL runs on.

        Returns:
            ``name (SELECT ...)``.

        Raises:
            ConfigurationError: The queryset is of another model, or filters, joins or limits its rows.
        """
        self.raise_if_not_of(model, editor.client)
        return f"{editor.quote(self.name)} ({self.get_query_sql(editor.client)})"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """How a migration file rebuilds the projection.

        Returns:
            The class path, no positional arguments, and the keyword arguments.
        """
        kwargs: dict[str, Any] = {"name": self.name, "query": RawSQLTerm(self.get_query_sql())}
        if read_columns := self.get_read_columns():
            kwargs["read_columns"] = read_columns
        return ClassPath.get(type(self)), [], kwargs
