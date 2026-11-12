from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.core.registries import Registries
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import QueryError, UnSupportedError
from hare.sql.builder.queries.query_builder import QueryBuilder

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification


class QuerySql:
    """Builds base selections and SQL previews from query descriptions using the execution dialect."""

    @staticmethod
    def _get_base_query(specification: QuerySpecification[Any]) -> QueryBuilder:
        """A fresh ``SELECT ... FROM`` the model's table, built for the connection the query runs on.

        Returns:
            The query builder.
        """
        meta = specification.model._meta
        connection = specification._connection if specification._connection is not None else meta.connection
        if connection.query_class is meta.query_class:
            return QuerySql._get_sampled_query(specification, copy(meta.basequery), connection)
        return QuerySql._get_sampled_query(specification, connection.query_class.from_(meta.basetable), connection)

    @staticmethod
    def _get_base_query_all_fields(specification: QuerySpecification[Any]) -> QueryBuilder:
        """``_get_base_query()`` selecting every column of the model.

        Returns:
            The query builder.
        """
        meta = specification.model._meta
        connection = specification._connection if specification._connection is not None else meta.connection
        if connection.query_class is meta.query_class:
            return QuerySql._get_sampled_query(specification, copy(meta.basequery_all_fields), connection)
        return QuerySql._get_sampled_query(
            specification, connection.query_class.from_(meta.basetable).select(*meta.db_fields), connection
        )

    @staticmethod
    def _get_sampled_query(
        specification: QuerySpecification[Any], query: QueryBuilder, connection: DatabaseClient
    ) -> QueryBuilder:
        """``query`` reading the sample of the model's table ``sample()`` asked for.

        Args:
            query: A ``SELECT ... FROM`` the model's table.
            connection: The connection the query runs on.

        Returns:
            The query - itself without a sample.

        Raises:
            UnSupportedError: The database can't read a sample of a table.
            QueryError: The model's table options allow no sample (``TableOptions.raise_if_unsampled()``).
        """
        table_sample = specification._table_sample
        if table_sample is None:
            return query
        if not connection.features.supports_table_sample:
            raise UnSupportedError(f"sample() needs TABLESAMPLE, which {connection.dialect} doesn't have")
        table_options = specification.model._meta.get_table_options(connection.dialect)
        if table_options is not None:
            table_options.raise_if_unsampled(specification.model)
        return query.sample(table_sample.method, table_sample.percent, table_sample.seed)

    @staticmethod
    def _get_explain_statements(
        specification: QuerySpecification[Any],
        statements: list[tuple[str, list[Any]]],
        output_format: str | None,
        options: dict[str, bool],
    ) -> list[tuple[str, list[Any]]]:
        """The EXPLAIN statement of each statement, in the dialect of the connection the query runs on.

        Args:
            statements: ``(sql, bound values)`` of each explained statement.
            output_format: The plan's output format, None for the dialect's default.
            options: The EXPLAIN options.

        Returns:
            ``(sql, bound values)`` of each EXPLAIN statement.
        """
        connection: DatabaseClient = specification._connection  # type: ignore[assignment]
        clauses = connection.dialect.clauses
        return [
            (clauses.get_explain_sql(sql, output_format, options, connection.features), values)
            for sql, values in statements
        ]

    @staticmethod
    def sql(
        specification: QuerySpecification[Any],
        parameters_inline: bool = False,
        *,
        explain: bool = False,
        output_format: str | None = None,
        **explain_options: bool,
    ) -> str:
        """The SQL the query runs, in the dialect of the connection it runs on.

        Args:
            parameters_inline: Whether values are rendered into the SQL instead of placeholders.
            explain: Return the ``EXPLAIN`` statement of the query instead - what ``explain()`` runs.
            output_format: The plan's output format with ``explain=True``.
            explain_options: The EXPLAIN options with ``explain=True``.

        Returns:
            The SQL; several statements are joined with ``;``.

        Raises:
            QueryError: ``output_format`` or an EXPLAIN option is given without ``explain=True``.
            UnSupportedError: The dialect has no such EXPLAIN format or option.
        """
        if not explain and (output_format is not None or explain_options):
            raise QueryError("output_format and EXPLAIN options apply only with explain=True")
        with Registries.bind():
            execution_query = specification._get_compiler()._get_execution_query()
            statements = execution_query._get_statements(parameters_inline)
            if explain:
                statements = QuerySql._get_explain_statements(
                    execution_query, statements, output_format, explain_options
                )
            return ";".join(sql for sql, _values in statements)

    @staticmethod
    def get_read_columns(specification: QuerySpecification[Any]) -> set[tuple[str, str]]:
        """The columns the query reads - in its joins and subqueries too - by the tables they belong to:
        what a view selecting it depends on.

        Returns:
            ``(table, column)`` of each column.
        """
        with Registries.bind():
            return specification._get_compiler()._get_execution_query()._get_read_columns()

    @staticmethod
    async def explain(
        specification: QuerySpecification[Any], output_format: str | None = None, **options: bool
    ) -> list[dict[str, Any]]:
        """Runs the query's ``EXPLAIN`` statement - the one ``sql(explain=True)`` returns - and
        returns the plan rows.

        Args:
            output_format: The plan's output format - one the dialect has (``text``, ``json``, ...),
                its own default when None.
            options: The EXPLAIN options supported by the selected connection.

        Returns:
            The plan rows as dicts of column name and value - the columns and their content depend
            on the database, never on its driver.

        Raises:
            UnSupportedError: The dialect has no such format or option.
        """
        execution_query = specification._get_compiler()._get_execution_query()
        connection: DatabaseClient = execution_query._connection  # type: ignore[assignment]
        statements = QuerySql._get_explain_statements(
            execution_query, execution_query._get_statements(parameters_inline=False), output_format, options
        )
        rows: list[dict[str, Any]] = []
        for sql, values in statements:
            rows.extend(dict(row) for row in (await connection.execute(sql, values))[1])
        return rows
