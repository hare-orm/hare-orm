from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import RawSQL
from hare.query.rows.model_rows import ModelRows
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql.terms.base.parameterizer import Parameterizer

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class RawSQLQuery(AwaitableQuery[TModel], Generic[TModel]):
    plannable = False

    __slots__ = ("_raw_sql",)

    def __init__(self, model: type[TModel], db: DatabaseClient, sql: str, params: Sequence[Any] = ()) -> None:
        super().__init__(model)
        # Constructed eagerly here, not lazily inside _execute(), so a %s-placeholder/params
        # count mismatch (see RawSQL.__init__) surfaces immediately at .raw(sql, params) call
        # time, not only once the query is later awaited.
        self._raw_sql = RawSQL(sql, params)
        self._apply_db(db)

    def _get_statements(self, params_inline: bool) -> list[tuple[str, list[Any]]]:
        """The raw SQL with its ``%s`` placeholders in the connection's own style.

        Args:
            params_inline: Whether the params are rendered into the SQL instead of bound.

        Returns:
            The statement and its bound values.
        """
        sql_context = self._db.query_class.SQL_CONTEXT
        if params_inline:
            return [(self._raw_sql.get_sql(sql_context), [])]
        parameterizer = Parameterizer()
        sql = self._raw_sql.get_sql(sql_context.copy(parameterizer=parameterizer))
        return [(sql, parameterizer.values)]

    async def _execute(self) -> Any:
        [(rendered_sql, values)] = self._get_statements(params_inline=False)
        _, rows = await self._db.execute(rendered_sql, values)
        if not rows:
            return []
        _model_column_names, annotation_names = self._get_row_column_names(list(rows[0].keys()))
        return await ModelRows(self.model, self._db, annotations=annotation_names).read_all(rows)

    def _get_row_column_names(self, column_names: list[str]) -> tuple[list[str], list[str]]:
        """Splits the raw query's columns into model columns and annotations - a column that is
        neither a column nor a field of the model becomes an attribute of each instance.

        Args:
            column_names: The result's column names, in order.

        Returns:
            The model column names and the annotation names.

        Raises:
            QueryError: A column name appears more than once.
            FieldError: The primary key column isn't selected.
        """
        duplicate_names = sorted({name for name in column_names if column_names.count(name) > 1})
        if duplicate_names:
            raise QueryError(
                f"raw() query returns the column(s) {duplicate_names} more than once - alias the "
                "extra ones (e.g. SELECT b.*, a.id AS author_pk ...), each column name must be unique."
            )
        meta = self.model._meta
        layout = meta.get_hydration_layout(self._db)
        missing_pk_columns = [
            meta.fields_db_projection[pk_name]
            for pk_name in meta.pk_attr_names
            if meta.fields_db_projection[pk_name] not in column_names and pk_name not in column_names
        ]
        if missing_pk_columns:
            raise FieldError(
                f"raw() query for {self.model.__name__} must select the primary key column(s) "
                f"{missing_pk_columns} - instances can't be built without it."
            )
        model_column_names = []
        annotation_names = []
        for column_name in column_names:
            if column_name in layout.entry_by_column or column_name in layout.entry_by_field_name:
                model_column_names.append(column_name)
            elif column_name not in meta.fields_map:
                annotation_names.append(column_name)
        return model_column_names, annotation_names

    def __await__(self) -> Generator[Any, None, list[TModel]]:
        return self._get_execution_query()._execute().__await__()
