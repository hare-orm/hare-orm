from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, cast

from hare.sql.builder.returned_value import ReturnedValue
from hare.sql.builder.tables.cte import Cte
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.sql_context import SqlContext
from hare.sql.terms.field import Field
from hare.sql.terms.select_reference import SelectReference
from hare.sql.terms.star import Star

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.term import Term


class QuerySqlRendering:
    """The SQL text of a built query: the WITH clause, the SELECT, INSERT, UPDATE and DELETE statements
    and each of their clauses, rendered in the context's namespace."""

    @staticmethod
    def get_returned_values(builder: QueryBuilder, sql_context: SqlContext) -> list[ReturnedValue]:
        """The values the statement's ``RETURNING`` returns.

        Args:
            builder: The query builder.
            sql_context: The context the statement renders in.

        Returns:
            Each returned term's SQL, with the name a plain column is read back by.
        """
        returning_context = sql_context.copy(with_namespace=bool(builder._update_table), with_alias=True)
        returned_values = []
        for term in builder._returns:
            column_name = (
                term.name if isinstance(term, Field) and not isinstance(term, Star) and not term.alias else None
            )
            returned_values.append(ReturnedValue(sql=term.get_sql(returning_context), column_name=column_name))
        return returned_values

    @staticmethod
    def select_insert_delete_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The SQL of a SELECT, an INSERT or a DELETE.

        Args:
            builder: The query builder.
            sql_context: The context the statement renders in.

        Returns:
            The statement.
        """
        querystring = QuerySqlRendering.with_sql(builder._with, sql_context) if builder._with else ""
        querystring += QuerySqlRendering.statement_start_sql(builder, sql_context)
        if builder._insert_table is not None and QuerySqlRendering.inserts_given_rows(builder):
            return querystring + QuerySqlRendering.inserted_rows_sql(builder, sql_context)
        if builder._from:
            querystring += QuerySqlRendering.from_sql(builder, sql_context)

        if builder._joins:
            querystring += " " + " ".join(join.get_sql(sql_context) for join in builder._joins)

        dialect_clauses = builder._dialect_clauses
        if dialect_clauses:
            querystring += sql_context.dialect.clauses.get_prewhere_sql(builder, sql_context)

        if builder._wheres:
            querystring += QuerySqlRendering.where_sql(builder, sql_context)

        if builder._groupbys or builder._havings or builder._orderbys:
            querystring += QuerySqlRendering.grouping_and_ordering_sql(builder, sql_context)

        if dialect_clauses:
            querystring += sql_context.dialect.clauses.get_limit_by_sql(builder, sql_context)

        querystring += builder._limit_offset_sql(sql_context)

        if builder._for_update:
            querystring += sql_context.dialect.clauses.get_row_lock_sql(builder, sql_context)
        return QuerySqlRendering.finish_statement_sql(builder, sql_context, querystring)

    @staticmethod
    def inserts_given_rows(builder: QueryBuilder) -> bool:
        """Whether the statement is an INSERT of the columns' defaults, of given values or of a rows'
        source - not of a SELECT.

        Args:
            builder: The query builder.

        Returns:
            True when it is.
        """
        return (
            not builder._delete_from
            and not builder._select_into
            and builder._insert_table is not None
            and bool(builder._default_values or builder._values or builder._rows_source_sql is not None)
        )

    @staticmethod
    def statement_start_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The start of a SELECT, an INSERT or a DELETE - up to its FROM.

        Args:
            builder: The query builder.
            sql_context: The context the statement renders in.

        Returns:
            ``DELETE``; ``INSERT INTO`` with its columns, and the SELECT it inserts unless it inserts
            given rows; or the SELECT, with ``INTO`` when it selects into a table.
        """
        if builder._delete_from:
            return "DELETE"
        if not builder._select_into and builder._insert_table:
            start_sql = QuerySqlRendering.insert_sql(builder, sql_context)
            if builder._columns:
                start_sql += QuerySqlRendering.columns_sql(builder, sql_context)
            if QuerySqlRendering.inserts_given_rows(builder):
                return start_sql
            return start_sql + " " + QuerySqlRendering.select_sql(builder, sql_context)
        start_sql = QuerySqlRendering.select_sql(builder, sql_context)
        if builder._insert_table:
            start_sql += QuerySqlRendering.into_sql(builder, sql_context)
        return start_sql

    @staticmethod
    def finish_statement_sql(builder: QueryBuilder, sql_context: SqlContext, querystring: str) -> str:
        """A statement with its end: the dialect's end clauses, the parentheses of a subquery, what
        it does on a conflict and its alias.

        Args:
            builder: The query builder.
            sql_context: The context the statement renders in.
            querystring: The statement up to its row locks.

        Returns:
            The statement.
        """
        if sql_context.subquery:
            querystring = f"({querystring}{sql_context.dialect.clauses.get_statement_end_sql(builder, sql_context)})"
        elif builder._dialect_clauses:
            querystring += sql_context.dialect.clauses.get_statement_end_sql(builder, sql_context)
        if builder._on_conflict:
            querystring += sql_context.dialect.clauses.get_on_conflict_sql(builder, sql_context)
        if sql_context.with_alias:
            return sql_context.format_alias_sql(querystring, builder.alias)
        return querystring

    @staticmethod
    def grouping_and_ordering_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The GROUP BY, HAVING and ORDER BY clauses of a statement.

        Args:
            builder: The query builder.
            sql_context: The context the statement renders in.

        Returns:
            The clauses the query has.
        """
        # Collected once for both the grouping and the ordering.
        selected_aliases = {select_term.alias for select_term in builder._selects}
        grouped_sql_by_term_id: dict[int, str] = {}
        clauses_sql = ""
        if builder._groupbys:
            clauses_sql += QuerySqlRendering.group_sql(builder, sql_context, selected_aliases, grouped_sql_by_term_id)
        if builder._havings:
            clauses_sql += QuerySqlRendering.having_sql(builder, sql_context)
        if builder._orderbys:
            clauses_sql += builder._orderby_sql(sql_context, selected_aliases, grouped_sql_by_term_id)
        return clauses_sql

    @staticmethod
    def query_references_table_name(query: Selectable | None, name: str) -> bool:
        """Whether ``query`` reads a table named ``name`` - a recursive CTE's step reads a plain
        ``Table(name)``. Every branch of a set operation is checked.
        """
        # Local import: the query builder module imports this module.
        from hare.sql.builder.queries.query_builder import QueryBuilder

        # Imported here: the modules import each other.
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        if isinstance(query, SetOperationQuery):
            branches = [query.base_query] + [branch for _, branch in query._set_operation]
            return any(QuerySqlRendering.query_references_table_name(branch, name) for branch in branches)
        if isinstance(query, QueryBuilder):
            tables = [*query._from, *(join.item for join in query._joins)]
            return any(getattr(table, "_table_name", None) == name for table in tables)
        return False

    @staticmethod
    def with_sql(with_clauses: list[Cte], sql_context: SqlContext) -> str:
        """Renders a `WITH [RECURSIVE] name AS (...), ...` clause - a staticmethod (not reading
        `self._with` directly) so `SetOperationQuery.get_sql()` can render its own hoisted CTE
        list through the exact same rendering, despite not being a `QueryBuilder` itself.

        Args:
            with_clauses: The CTEs to render, in declaration order.
            sql_context: The SQL rendering context.

        Returns:
            The rendered `WITH` clause, including a trailing space.
        """
        recursive = any(
            QuerySqlRendering.query_references_table_name(with_.query, with_.alias) for with_ in with_clauses
        )

        as_context = sql_context.copy(subquery=False, with_alias=False)
        return f"WITH {'RECURSIVE ' if recursive else ''}" + ",".join(
            sql_context.quote_alias(clause.alias)
            + ("(" + ",".join([term.get_sql(sql_context) for term in clause.terms]) + ")" if clause.terms else "")
            + " AS ("
            + clause.get_sql(as_context)
            + ") "
            for clause in with_clauses
        )

    @staticmethod
    def select_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        select_context = sql_context.copy(subquery=True, with_alias=True)
        if sql_context.names_qualified_columns:
            select = ",".join(
                QuerySqlRendering.get_named_column_sql(term, select_context) for term in builder._selects
            )
        else:
            select = ",".join(term.get_sql(select_context) for term in builder._selects)
        return f"SELECT {sql_context.dialect.clauses.get_distinct_sql(builder, sql_context)}{select}"

    @staticmethod
    def get_named_column_sql(term: Term, sql_context: SqlContext) -> str:
        """A selected term's SQL - a column written with its table (``"t"."c"``) aliased by its own name.

        Args:
            term: The selected term.
            sql_context: The context it renders in.

        Returns:
            The SQL.
        """
        sql = term.get_sql(sql_context)
        if isinstance(term, Field) and not isinstance(term, Star) and term.alias is None:
            name = term.name
            if sql != sql_context.quote(name):
                return sql_context.format_alias_sql(sql, name)
        return sql

    @staticmethod
    def insert_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        table = builder._insert_table.get_sql(sql_context)  # type:ignore[union-attr]
        return f"INSERT INTO {table}"

    @staticmethod
    def inserted_rows_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The rows an INSERT without a SELECT writes - the columns' defaults, the given values or
        the rows' source - with what it does on a conflict.

        Args:
            builder: The query builder.
            sql_context: The context the statement renders in.

        Returns:
            The rest of the statement.
        """
        if builder._default_values:
            rows_sql = QuerySqlRendering.default_values_sql()
        elif builder._values:
            rows_sql = QuerySqlRendering.values_sql(builder, sql_context)
        else:
            rows_sql = f" {builder._rows_source_sql}"
        if builder._on_conflict:
            rows_sql += sql_context.dialect.clauses.get_on_conflict_sql(builder, sql_context)
        return rows_sql

    @staticmethod
    def update_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        table = builder._update_table.get_sql(sql_context)  # type:ignore[union-attr]
        return f"UPDATE {table}"

    @staticmethod
    def columns_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """SQL for the columns clause of an INSERT query.

        Args:
            builder: The query builder.
            sql_context: The rendering state of the statement.
        """
        # The columns of an INSERT name no table - there is only the one inserted into.
        sql_context = sql_context.copy(with_namespace=False)
        return f" ({','.join(term.get_sql(sql_context) for term in builder._columns)})"

    @staticmethod
    def values_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        values_context = sql_context.copy(subquery=True, with_alias=True)
        if builder._insert_rows_by_select:
            return " " + " UNION ALL ".join(
                "SELECT " + ",".join(term.get_sql(values_context) for term in row) for row in builder._values
            )
        rows_sql = "),(".join(",".join(term.get_sql(values_context) for term in row) for row in builder._values)
        return f" VALUES ({rows_sql})"

    @staticmethod
    def default_values_sql() -> str:
        return " DEFAULT VALUES"

    @staticmethod
    def into_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        into_context = sql_context.copy(with_alias=False)
        return f" INTO {builder._insert_table.get_sql(into_context)}"  # type:ignore[union-attr]

    @staticmethod
    def from_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        from_context = sql_context.copy(subquery=True, with_alias=True)
        selectables = [clause.get_sql(from_context) for clause in builder._from]
        if (builder._table_sample is not None or builder._dialect_clauses) and selectables:
            selectables[0] += sql_context.dialect.clauses.get_main_table_suffix_sql(builder, sql_context)
        return f" FROM {','.join(selectables)}"

    @staticmethod
    def where_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        where_context = sql_context.copy(subquery=True)
        wheres = cast("QueryBuilder", builder._wheres)
        return f" WHERE {wheres.get_sql(where_context)}"

    @staticmethod
    def group_sql(
        builder: QueryBuilder,
        sql_context: SqlContext,
        # builtins.set - QueryBuilder.set() shadows the builtin name in this class body.
        selected_aliases: builtins.set[str | None] | None = None,
        grouped_sql_by_term_id: dict[int, str] | None = None,
    ) -> str:
        """Renders the GROUP BY clause. A term selected under an alias is grouped by the alias when
        ``groupby_alias`` is set.

        Args:
            builder: The query builder.
            sql_context: The rendering state of the statement.
            selected_aliases: The selected aliases, when the caller already has them.
            grouped_sql_by_term_id: Receives each grouped term's SQL by term id, for
                ``_orderby_sql()``.
        """
        clauses = []
        if selected_aliases is None:
            selected_aliases = {select_term.alias for select_term in builder._selects}
        for field in builder._groupbys:
            if (
                isinstance(field, SelectReference)
                and (select_position := field.get_select_position(builder._selects)) is not None
            ):
                clauses.append(str(select_position))
            elif (alias := field.alias) and alias in selected_aliases:
                if sql_context.groupby_alias:
                    clauses.append(sql_context.quote_alias(alias))
                else:
                    for select in builder._selects:
                        if select.alias == alias:
                            clauses.append(select.get_sql(sql_context))
                            break
            else:
                field_sql = field.get_sql(sql_context.copy(with_alias=False, subquery=True))
                if grouped_sql_by_term_id is not None:
                    grouped_sql_by_term_id[id(field)] = field_sql
                clauses.append(field_sql)

        return f" GROUP BY {','.join(clauses)}"

    @staticmethod
    def having_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        # subquery=True as for WHERE - a subquery in HAVING (EXISTS/scalar) needs its parentheses.
        having = builder._havings.get_sql(sql_context.copy(subquery=True))  # type:ignore[union-attr]
        return f" HAVING {having}"

    @staticmethod
    def set_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        field_context = sql_context.copy(with_namespace=False)
        value_context = sql_context.copy(subquery=True)
        assignments_sql = ",".join(
            f"{field.get_sql(field_context)}={value.get_sql(value_context)}" for field, value in builder._updates
        )
        return f" SET {assignments_sql}"
