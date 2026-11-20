from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.classes.value_descriptions import ValueDescriptions
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.schema.messages import PROJECTION_WRONG_MODEL
from hare.dialects.clickhouse.schema.schema_objects.clickhouse_projection_compiler import ClickhouseProjectionCompiler
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from typing import Self

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
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
    _binding: ModelBinding | None = dataclasses.field(default=None, init=False, compare=False, repr=False)
    _source_model: type[Model] | None = dataclasses.field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigurationError(f"ClickhouseProjection.name must be a non-empty string, got {self.name!r}")
        if isinstance(self.read_columns, str) or not all(  # type: ignore[unreachable]
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
        if isinstance(self.query, QuerySet) and (
            self.query._context is not None or self.query._connection is not None
        ):
            object.__setattr__(self, "_source_model", self.query.model)
            sql, columns = ClickhouseProjectionCompiler.compile(self)
            object.__setattr__(self, "query", RawSQLTerm(sql))
            object.__setattr__(self, "read_columns", columns)

    def for_binding(self, binding: ModelBinding) -> Self:
        """Keep structural identity; compile bound SQL once and replay templates at compilation."""
        if self._source_model is not None and self._source_model is not binding._model:
            raise ConfigurationError(
                PROJECTION_WRONG_MODEL.format(
                    model=binding._model.__name__,
                    name=self.name,
                    query_model=self._source_model.__name__,
                )
            )
        if isinstance(self.query, RawSQLTerm):
            return self
        bound = dataclasses.replace(self)
        object.__setattr__(bound, "_binding", binding)
        return bound

    def get_queryset(self) -> QuerySet[Any, Any] | None:
        """The queryset the projection selects, None for raw SQL.

        Raises:
            ConfigurationError: A recorded model query has no declaring model.
        """
        from hare.query.queryset.model_query import ModelQuery

        return ModelQuery.get_declared_queryset(self.query, binding=self._binding)

    def get_query_sql(self, client: DatabaseClient | None = None) -> str:
        """Compile a SELECT without FROM; raw SQL is returned verbatim."""
        return ClickhouseProjectionCompiler.compile(self, client)[0]

    def get_read_columns(self) -> tuple[str, ...]:
        """Read columns from the same compilation that produces the projection SQL."""
        return ClickhouseProjectionCompiler.compile(self)[1]

    def with_sql_query(
        self, binding: ModelBinding | None, client: DatabaseClient | None = None
    ) -> ClickhouseProjection:
        """Snapshot SQL and dependencies, preserving the source-model check without resources."""
        sql, read_columns = ClickhouseProjectionCompiler.compile(self, client, binding)
        if isinstance(self.query, RawSQLTerm) and self._binding is None:
            return self
        return dataclasses.replace(self, query=RawSQLTerm(sql), read_columns=read_columns)

    def get_definition_sql(self, binding: ModelBinding, editor: BaseSchemaEditor) -> str:
        """Render ADD PROJECTION's name and SELECT after one isolated compilation."""
        sql, _ = ClickhouseProjectionCompiler.compile(self, editor.client, binding)
        return f"{editor.quote(self.name)} ({sql})"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Snapshot SQL and dependencies together for declaration serialization."""
        sql, read_columns = ClickhouseProjectionCompiler.compile(self)
        kwargs: dict[str, Any] = {"name": self.name, "query": RawSQLTerm(sql)}
        if read_columns:
            kwargs["read_columns"] = read_columns
        return ClassPath.get(type(self)), [], kwargs
