from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import RawSQL
from hare.query.plans.statement.raw_sql_plan import RawSQLPlan
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class RawSQLQuery(AwaitableQuery[TModel], Generic[TModel]):
    """A ``.raw()`` query. The text and the types of its parameters on a connection keep a plan
    (``RawSQLPlan``) - another call with them binds its parameters into the text rendered once."""

    plannable = False

    __slots__ = ("_sql", "_parameters", "_raw_sql", "_raw_sql_plan")

    def __init__(
        self, model: type[TModel], connection: DatabaseClient, sql: str, parameters: Sequence[Any] = ()
    ) -> None:
        super().__init__(model)
        self._sql = sql
        self._parameters = parameters
        self._apply_connection(connection)
        self._raw_sql_plan: RawSQLPlan | None = StatementPlans.raw_sql_plans.get(self._get_raw_sql_plan_key())
        # A text and parameter types a plan was kept for were checked when it was made. Any other
        # is checked here, not once the query is awaited, so a %s-placeholder/parameters count
        # mismatch (see RawSQL.__init__) surfaces at the .raw(sql, parameters) call.
        self._raw_sql: RawSQL | None = RawSQL(sql, parameters) if self._raw_sql_plan is None else None

    def _get_raw_sql_plan_key(self) -> tuple[Any, ...]:
        """The key of the query's plan in ``StatementPlans.raw_sql_plans``.

        Returns:
            The model, the connection, its query class, the text and the parameters' types.
        """
        connection = self._connection
        return (
            self.model,
            connection.connection_alias,
            connection.query_class,
            self._sql,
            tuple([type(parameter) for parameter in self._parameters]),
        )

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        """The raw SQL with its ``%s`` placeholders in the connection's own style - on the query's
        plan when it has one, and kept as its plan otherwise.

        Args:
            parameters_inline: Whether the params are rendered into the SQL instead of bound.

        Returns:
            The statement and its bound values.
        """
        raw_sql_plan = self._raw_sql_plan
        if raw_sql_plan is not None and not parameters_inline:
            values = raw_sql_plan.bind(self._parameters)
            if values is not None:
                StatementPlans.count_hit()
                return [(raw_sql_plan.sql, values)]
        raw_sql = self._raw_sql if self._raw_sql is not None else RawSQL(self._sql, self._parameters)
        sql_context = self._connection.query_class.SQL_CONTEXT
        if parameters_inline:
            return [(raw_sql.get_sql(sql_context), [])]
        parameterizer = RecordingParameterizer()
        sql = raw_sql.get_sql(sql_context.copy(parameterizer=parameterizer))
        # A parameter written into the text as a literal makes the text hold its value.
        if not parameterizer.literal_sources:
            indexes_by_parameter_id = {id(parameter): index for index, parameter in enumerate(raw_sql.parameters)}
            parameter_indexes = [indexes_by_parameter_id.get(id(source)) for source in parameterizer.sources]
            if None not in parameter_indexes:
                StatementPlans.raw_sql_plans[self._get_raw_sql_plan_key()] = RawSQLPlan(
                    sql, cast("tuple[int, ...]", tuple(parameter_indexes))
                )
        return [(sql, parameterizer.values)]

    async def _execute(self) -> Any:
        [(rendered_sql, values)] = self._get_statements(parameters_inline=False)
        connection = self._connection
        result = await connection.execute(
            rendered_sql, values, rows_by_position=connection.features.supports_positional_rows
        )
        if not result.rows:
            return []
        column_names = result.column_names
        annotation_names_key = (self.model, connection.connection_alias, connection.dialect, column_names)
        annotation_names = StatementPlans.raw_sql_annotation_names.get(annotation_names_key)
        if annotation_names is None:
            _model_column_names, annotation_names = self._get_row_column_names(list(column_names))
            StatementPlans.raw_sql_annotation_names[annotation_names_key] = annotation_names
        return await ModelRows(self.model, connection, annotations=annotation_names).read_all(result)

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
        layout = meta.get_hydration_layout(self._connection)
        missing_pk_columns = [
            meta.fields_db_projection[pk_name]
            for pk_name in meta.primary_key_attribute_names
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
