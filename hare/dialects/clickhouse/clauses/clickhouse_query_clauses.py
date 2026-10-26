from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import copy
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.dialects.clickhouse.clauses.constants import (
    CLICKHOUSE_BULK_LOAD_STATEMENT_TEMPLATE,
    CLICKHOUSE_CONSTANT_KEY_SET_MUTATION_SETTINGS_SQL,
    CLICKHOUSE_DERIVED_TABLE_TEMPLATE,
    CLICKHOUSE_DISTRIBUTED_MUTATION_READ_MESSAGE,
    CLICKHOUSE_EMPTY_CONTAINMENT_PATTERN_SQL,
    CLICKHOUSE_GLOBAL_PREFIX,
    CLICKHOUSE_JOIN_KEYWORDS,
    CLICKHOUSE_JOINING_SUBQUERY_SETTINGS,
    CLICKHOUSE_ROW_KEY_COLUMNS_TEMPLATE,
    CLICKHOUSE_SET_OPERATION_SQL,
    CLICKHOUSE_UNBOUNDED_LIMIT_SQL,
)
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.enums import ClickhouseClause
from hare.dialects.clickhouse.query.clickhouse_sql_context import ClickhouseSqlContext
from hare.exceptions import QueryError, UnSupportedError
from hare.query.enums import TableSampleMethod

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.enums import SetOperation
    from hare.sql.sql_context import SqlContext
    from hare.sql.tables.table import Table


class ClickhouseQueryClauses(QueryClauses):
    """ClickHouse's clauses - an UPDATE is an ``ALTER TABLE ... UPDATE`` mutation and a DELETE a
    lightweight delete, both run to the end before the statement returns (the session's
    ``mutations_sync``/``lightweight_deletes_sync``); a LIKE pattern escapes with a backslash, its
    only escape character."""

    join_keywords = CLICKHOUSE_JOIN_KEYWORDS

    def get_bulk_load_statement_sql(self, table: str, columns: Sequence[str]) -> str:
        return CLICKHOUSE_BULK_LOAD_STATEMENT_TEMPLATE.format(table=table, columns=", ".join(columns))

    def get_statement_context(self, builder: QueryBuilder, sql_context: SqlContext) -> SqlContext:
        # The aggregates of a grouped statement read at least one row each - their renderer asks. A
        # read of a Distributed table runs on every shard - its joins and IN subqueries are GLOBAL.
        if isinstance(sql_context, ClickhouseSqlContext):
            return sql_context.copy(
                rows_are_grouped=bool(builder._groupbys),
                reads_distributed_table=self.reads_distributed_table(builder),
            )
        return sql_context

    def reads_distributed_table(self, builder: QueryBuilder) -> bool:
        """Whether a statement reads its rows from a ``Distributed`` table - a ``SELECT`` of a model
        declaring ``distributed_over``.

        Args:
            builder: The statement.

        Returns:
            Whether it does.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.tables.table import Table

        if not builder._from or builder._update_table is not None or builder._delete_from:
            return False
        source = builder._from[0]
        # A derived table's own statement reads the Distributed table.
        return (
            isinstance(source, Table)
            and self.get_model_table_options(source, self.dialect).distributed_over is not None
        )

    def get_distributed_read_prefix_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_GLOBAL_PREFIX if getattr(sql_context, "reads_distributed_table", False) else ""

    def get_statement_end_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The statement's ``SETTINGS`` - those of ``settings()``, and ``join_use_nulls`` of a
        subquery joining tables, which a mutation's condition runs without the session's settings.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause with its leading space, ``""`` for none.
        """
        settings = builder._dialect_clauses.get(ClickhouseClause.SETTINGS)
        if sql_context.subquery and builder._joins:
            settings = {**CLICKHOUSE_JOINING_SUBQUERY_SETTINGS, **(settings or {})}
        if not settings:
            return ""
        return " SETTINGS " + ", ".join(
            f"{name} = {ClickhouseTableOptions.get_setting_sql(value)}" for name, value in settings.items()
        )

    def get_main_table_suffix_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``FINAL`` (``final()``), then the sample: ``SAMPLE <share>`` of ``sample()`` or
        ``SAMPLE <rows>`` of ``sample_rows()``, with the ``OFFSET`` of ``sample_offset()``.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The text with its leading space, ``""`` for none.

        Raises:
            UnSupportedError: ``sample()`` asks for a seed, the ``SYSTEM`` method or no rows.
            QueryError: ``sample_offset()`` without a sample.
        """
        clauses = builder._dialect_clauses
        sql = " FINAL" if clauses.get(ClickhouseClause.FINAL) else ""
        table_sample = builder._table_sample
        sample_rows = clauses.get(ClickhouseClause.SAMPLE_ROWS)
        if table_sample is not None:
            method, percent, seed = table_sample
            if method != TableSampleMethod.BERNOULLI or seed is not None:
                raise UnSupportedError(
                    "ClickHouse samples the rows of a table by its sample key - sample() takes no method but "
                    "BERNOULLI and no seed there: the same share reads the same rows"
                )
            if not percent:
                raise UnSupportedError(
                    "ClickHouse reads a SAMPLE 0 as the whole table - sample(0) reads no rows, use none()"
                )
            sql += f" SAMPLE {self.get_share_sql(percent)}"
        elif sample_rows is not None:
            sql += f" SAMPLE {sample_rows}"
        sample_offset = clauses.get(ClickhouseClause.SAMPLE_OFFSET)
        if sample_offset is not None:
            if table_sample is None and sample_rows is None:
                raise QueryError("sample_offset() moves the sample of sample() or sample_rows() - there is none")
            sql += f" OFFSET {self.get_share_sql(sample_offset)}"
        return sql

    @staticmethod
    def get_share_sql(percent: float) -> str:
        """A share of ``SAMPLE`` as a decimal fraction - ClickHouse takes no exponent there.

        Args:
            percent: The share in percent.

        Returns:
            The fraction.
        """
        return format(Decimal(repr(percent)).scaleb(-2).normalize(), "f")

    def get_subquery_source_sql(self, builder: QueryBuilder, source: Any, sql_context: SqlContext) -> str:
        # An aliased table answers to its own name too - a correlated subquery over the table its
        # enclosing query reads would read the name as its own table. Its rows come from a derived
        # table: the name is the enclosing query's alone.
        if builder._table_sample is not None or builder._dialect_clauses:
            return cast("str", source.get_sql(sql_context))
        return self.get_joined_source_sql(source, sql_context)

    def get_joined_source_sql(self, source: Any, sql_context: SqlContext) -> str:
        # An aliased table answers to its own name too - see get_subquery_source_sql(). Local import:
        # the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.tables.table import Table

        if not isinstance(source, Table) or not source.alias:
            return cast("str", source.get_sql(sql_context))
        unaliased_table = copy(source)
        unaliased_table.alias = None
        table_sql = unaliased_table.get_sql(sql_context)
        return sql_context.format_alias_sql(CLICKHOUSE_DERIVED_TABLE_TEMPLATE.format(table=table_sql), source.alias)

    def get_prewhere_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        criterion = builder._dialect_clauses.get(ClickhouseClause.PREWHERE)
        if criterion is None:
            return ""
        return f" PREWHERE {criterion.get_sql(sql_context.copy(subquery=True))}"

    def get_group_by_modifier_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        return " WITH TOTALS" if builder._dialect_clauses.get(ClickhouseClause.WITH_TOTALS) else ""

    def get_limit_by_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        limit_by = builder._dialect_clauses.get(ClickhouseClause.LIMIT_BY)
        if limit_by is None:
            return ""
        limit, offset, terms = limit_by
        terms_context = sql_context.copy(with_alias=False, subquery=True)
        offset_sql = f" OFFSET {offset}" if offset else ""
        return f" LIMIT {limit}{offset_sql} BY {', '.join(term.get_sql(terms_context) for term in terms)}"

    def get_select_all_except_sql(self, quoted_columns: Sequence[str]) -> str:
        return f"SELECT * EXCEPT ({','.join(quoted_columns)})"

    def get_update_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``ALTER TABLE t UPDATE column = value, ... WHERE ...`` - every row for no condition.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The statement.

        Raises:
            UnSupportedError: The update joins or reads other tables, orders or limits its rows, or
                has a ``WITH`` clause; it changes a distributed table and its condition or values read a
                table that isn't one.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering

        if builder._joins or builder._from or builder._orderbys or builder._limit is not None or builder._with:
            raise UnSupportedError(
                "A ClickHouse UPDATE mutation changes the rows of one table matching a condition - it joins, "
                "orders, limits and reads a WITH clause of nothing"
            )
        self.raise_if_server_rows_read(
            builder._update_table, [builder._wheres, *(value for _, value in builder._updates)]
        )
        field_context = sql_context.copy(with_namespace=False)
        value_context = sql_context.copy(subquery=True)
        assignments_sql = ", ".join(
            f"{field.get_sql(field_context)} = {value.get_sql(value_context)}" for field, value in builder._updates
        )
        where_sql = QuerySqlRendering.where_sql(builder, sql_context) if builder._wheres else " WHERE 1"
        table_sql = builder._update_table.get_sql(sql_context)  # type: ignore[union-attr]
        update_sql = self.get_update_head_sql(builder._update_table, table_sql)
        return f"{update_sql} {assignments_sql}{where_sql}{self.get_statement_end_sql(builder, sql_context)}"

    def get_delete_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``DELETE FROM t WHERE ...`` - every row for no condition.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The statement.

        Raises:
            UnSupportedError: The delete joins other tables, orders or limits its rows, or has a
                ``WITH`` clause; it deletes from a distributed table and its condition reads a table that
                isn't one.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering

        if (
            builder._joins
            or len(builder._from) != 1
            or builder._orderbys
            or builder._limit is not None
            or builder._with
        ):
            raise UnSupportedError(
                "A ClickHouse DELETE removes the rows of one table matching a condition - it joins, orders, "
                "limits and reads a WITH clause of nothing"
            )
        self.raise_if_server_rows_read(builder._from[0], [builder._wheres])
        where_sql = QuerySqlRendering.where_sql(builder, sql_context) if builder._wheres else " WHERE 1"
        table_sql = builder._from[0].get_sql(sql_context)
        statement_end_sql = self.get_statement_end_sql(builder, sql_context)
        # The table and the condition are hare's own rendering; values are inlined literals. A
        # lightweight DELETE fails on a set operation in its condition (the server's mutation of it
        # refuses a UNION it hasn't normalized) - an ALTER TABLE ... DELETE takes one.
        if self.reads_set_operation(builder._wheres):
            delete_sql = f"ALTER TABLE {table_sql} DELETE{where_sql}{statement_end_sql}"
        else:
            delete_sql = f"DELETE FROM {table_sql}{where_sql}{statement_end_sql}"  # nosec B608
        return delete_sql

    @staticmethod
    def get_model_table_options(table: Any, dialect: Dialect) -> ClickhouseTableOptions:
        """The ClickHouse options of the table a statement writes.

        Args:
            table: The written table.
            dialect: The dialect.

        Returns:
            The options of the table's model - the dialect's defaults for a table of no model, and
            for a model declaring none.
        """
        return ClickhouseTableOptions.get_of_dialect(getattr(table, "table_options", ()), dialect)

    def raise_if_server_rows_read(self, table: Any, terms: Sequence[Any]) -> None:
        """Refuses a change of a distributed table's rows whose condition or values read a table that isn't
        distributed - each server changes its own rows of the table, reading its own rows of that one, so a
        row matching by the rows of another server is left unchanged, with no error.

        Args:
            table: The changed table.
            terms: The condition and the new values.

        Raises:
            UnSupportedError: The table is distributed and a term reads a table that isn't.
        """
        if self.get_model_table_options(table, self.dialect).distributed_over is None:
            return
        read_tables = {
            read_table.get_name_without_alias(): None
            for term in terms
            for read_table in self.get_read_tables(term)
            if self.get_model_table_options(read_table, self.dialect).distributed_over is None
        }
        if read_tables:
            raise UnSupportedError(
                CLICKHOUSE_DISTRIBUTED_MUTATION_READ_MESSAGE.format(
                    table=table.get_name_without_alias(), read_tables=", ".join(read_tables)
                )
            )

    @classmethod
    def get_read_tables(cls, term: Any) -> list[Any]:
        """The tables a term's subqueries read - in any of their clauses, at any depth.

        Args:
            term: A condition, a value or a query.

        Returns:
            The tables.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.query.expressions.subqueries.subquery import Subquery
        from hare.sql.builder.queries.query_builder import QueryBuilder
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery
        from hare.sql.builder.tables.table import Table

        if isinstance(term, Table):
            return [term]
        if isinstance(term, Subquery):
            nested_query = term.get_nested_query()
            return [] if nested_query is None else cls.get_read_tables(nested_query)
        if isinstance(term, SetOperationQuery):
            queries = [
                term.base_query,
                *(query for _, query in term._set_operation),
                *(cte.query for cte in term._with),
            ]
            return [table for query in queries for table in cls.get_read_tables(query)]
        if isinstance(term, QueryBuilder):
            limit_by = term._dialect_clauses.get(ClickhouseClause.LIMIT_BY)
            parts = [
                *term._from,
                *(join.item for join in term._joins),
                *(getattr(join, "criterion", None) for join in term._joins),
                *(cte.query for cte in term._with),
                *term._selects,
                term._dialect_clauses.get(ClickhouseClause.PREWHERE),
                term._wheres,
                *term._groupbys,
                term._havings,
                *(ordering_term for ordering_term, _ in term._orderbys),
                *(limit_by[2] if limit_by is not None else ()),
            ]
            return [
                table for part in parts if part is not None and part is not term for table in cls.get_read_tables(part)
            ]
        if not hasattr(term, "nodes_"):
            return []
        return [
            table
            for node in term.nodes_()
            if node is not term and isinstance(node, (Subquery, QueryBuilder, SetOperationQuery))
            for table in cls.get_read_tables(node)
        ]

    def get_update_head_sql(self, table: Any, table_sql: str) -> str:
        """How an update of a table begins - a lightweight ``UPDATE`` of a table declaring
        ``lightweight_updates``, else an ``ALTER TABLE ... UPDATE`` mutation.

        Args:
            table: The updated table.
            table_sql: Its SQL.

        Returns:
            The words before the assignments.
        """
        if self.get_model_table_options(table, self.dialect).lightweight_updates:
            return f"UPDATE {table_sql} SET"
        return f"ALTER TABLE {table_sql} UPDATE"

    @classmethod
    def reads_set_operation(cls, term: Any) -> bool:
        """Whether a condition reads a set operation - ``UNION``, ``INTERSECT``, ``EXCEPT`` - in a
        subquery at any depth.

        Args:
            term: The condition.

        Returns:
            Whether it does.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.query.expressions.subqueries.subquery import Subquery
        from hare.sql.builder.queries.query_builder import QueryBuilder
        from hare.sql.builder.queries.set_operation_query import SetOperationQuery

        if not hasattr(term, "nodes_"):
            return False
        for node in term.nodes_():
            if isinstance(node, SetOperationQuery):
                return True
            # A subquery expression holds the query it was built into.
            if isinstance(node, Subquery) and cls.reads_set_operation(node.get_nested_query()):
                return True
            if isinstance(node, QueryBuilder):
                parts = [*node._from, *(join.item for join in node._joins), node._wheres]
                if any(part is not node and cls.reads_set_operation(part) for part in parts):
                    return True
        return False

    def get_typed_placeholder_template(self, placeholder_template: str, cast_type: str | None) -> str:
        # ClickHouse's type names are written in their own case.
        if cast_type is None:
            return placeholder_template
        return f"CAST({placeholder_template} AS {cast_type})"

    def get_values_table_columns_sql(self, column_names: Sequence[str]) -> str:
        """The columns of the rows ``get_update_from_values_sql()`` reads - each row is a tuple, its
        columns read by position.

        Args:
            column_names: The names of the columns, ``c<N>``.

        Returns:
            The names, comma-separated - the rows are read by them.
        """
        return ", ".join(column_names)

    def get_update_from_values_sql(
        self,
        *,
        with_sql: str,
        table: Table,
        table_sql: str,
        assignments: Sequence[tuple[str, str]],
        values_columns_sql: str,
        values_sql: str,
        values_alias_sql: str,
        matches: Sequence[tuple[str, str]],
        condition_sql: str | None,
        returned: Sequence[ReturnedValue],
    ) -> str:
        """An UPDATE mutation writing each row of a list into the table row it matches - the rows
        are an array of tuples, a column's new value the matching tuple's element.

        Args:
            with_sql: The ``WITH`` clause, or an empty string.
            table: The updated table.
            table_sql: Its SQL.
            assignments: ``(column, value SQL)`` per assigned column - the value names a row column or
                is an expression of the table's columns.
            values_columns_sql: The rows' column names (``get_values_table_columns_sql()``).
            values_sql: The rows, ``(...), (...)``.
            values_alias_sql: The alias the value SQL names the rows by.
            matches: ``(table column SQL, row column SQL)`` pairs a row is matched by.
            condition_sql: A further condition on the updated rows, or None.
            returned: The returned values - none on ClickHouse.

        Returns:
            The statements.

        Raises:
            UnSupportedError: Values are returned, or the update has a ``WITH`` clause.
        """
        if returned or with_sql:
            raise UnSupportedError("A ClickHouse UPDATE mutation returns nothing and reads no WITH clause")
        column_names = [name.strip() for name in values_columns_sql.split(",")]

        def get_row_element_number(value_sql: str) -> int:
            # alias.c<N> -> the position of the column in a row tuple, from 1.
            return column_names.index(value_sql.rsplit(".", 1)[-1]) + 1

        def get_row_element_sql(value_sql: str) -> str:
            return f"row.{get_row_element_number(value_sql)}"

        rows_sql = f"[{values_sql}]"
        # A mutation names the columns of its one table unqualified. A row is found by comparing
        # each key column - a tuple compares only with a tuple of the same element types.
        table_prefix = f"{table_sql}."
        match_sql = " AND ".join(
            f"{get_row_element_sql(value)} = {column.removeprefix(table_prefix)}" for column, value in matches
        )
        position_sql = f"arrayFirstIndex(row -> {match_sql}, {rows_sql})"
        row_prefix = f"{values_alias_sql}."
        assignments_sql = ", ".join(
            f"{column} = tupleElement(arrayElement({rows_sql}, {position_sql}), {get_row_element_number(value)})"
            if value.startswith(row_prefix)
            else f"{column} = {value.replace(table_prefix, '')}"
            for column, value in assignments
        )
        # The rows' keys as a set first, which skips every other table row cheaply - the search of
        # the rows runs for the matching ones alone.
        key_columns_sql = ", ".join(column.removeprefix(table_prefix) for column, _value in matches)
        row_keys_sql = ", ".join(get_row_element_sql(value) for _column, value in matches)
        if len(matches) == 1:
            key_set_sql = f"(SELECT arrayJoin(arrayMap(row -> tuple({row_keys_sql}), {rows_sql})))"
        else:
            key_set_sql = CLICKHOUSE_ROW_KEY_COLUMNS_TEMPLATE.format(row_keys=row_keys_sql, rows=rows_sql)
        where_sql = f" WHERE tuple({key_columns_sql}) IN {key_set_sql} AND {position_sql} > 0"
        if condition_sql:
            where_sql += f" AND ({condition_sql.replace(table_prefix, '')})"
        # The key set is a subquery of the rows' own values - the same on every replica, which a
        # replicated table would refuse as maybe not.
        return (
            f"{self.get_update_head_sql(table, table_sql)} {assignments_sql}{where_sql}"
            f"{CLICKHOUSE_CONSTANT_KEY_SET_MUTATION_SETTINGS_SQL}"
        )

    def get_limit_offset_sql(self, limit_sql: str | None, offset_sql: str | None) -> str:
        """``LIMIT ... OFFSET ...`` - ClickHouse's ISO ``FETCH FIRST`` needs an ``ORDER BY``.

        Args:
            limit_sql: The SQL of the most rows, None for no bound.
            offset_sql: The SQL of the rows skipped, None for none.

        Returns:
            The clauses.
        """
        if limit_sql is None and offset_sql is not None:
            limit_sql = CLICKHOUSE_UNBOUNDED_LIMIT_SQL
        sql = f" LIMIT {limit_sql}" if limit_sql is not None else ""
        if offset_sql is not None:
            sql += f" OFFSET {offset_sql}"
        return sql

    def get_set_operation_sql(self, set_operation: SetOperation) -> str:
        """A set operation dropping repeated rows written with ``DISTINCT``, which ClickHouse needs.

        Args:
            set_operation: The operation.

        Returns:
            Its keyword.
        """
        return CLICKHOUSE_SET_OPERATION_SQL.get(set_operation, set_operation.value)

    def get_like_expression_pattern_sql(self, pattern_sql: str) -> str:
        # ClickHouse matches no empty string by '%%' - the pattern of an empty text's containment.
        return CLICKHOUSE_EMPTY_CONTAINMENT_PATTERN_SQL.format(pattern=pattern_sql)

    def get_like_escape_sql(self, escape_sql: str) -> str:
        """The backslash, ClickHouse's own escape character of a pattern, is written as nothing.

        Args:
            escape_sql: The ``ESCAPE`` clause of the pattern.

        Returns:
            An empty string.

        Raises:
            UnSupportedError: The pattern escapes with another character.
        """
        # Local import: the filters package imports the dialects, which load this module.
        from hare.sql.constants import DEFAULT_LIKE_ESCAPE_CLAUSE

        if escape_sql != DEFAULT_LIKE_ESCAPE_CLAUSE:
            raise UnSupportedError(f"A ClickHouse LIKE pattern escapes with a backslash only, not{escape_sql}")
        return ""

    def get_explain_sql(
        self, sql: str, output_format: str | None, options: Mapping[str, bool], features: Features
    ) -> str:
        if output_format is not None or options:
            raise UnSupportedError("ClickHouse's EXPLAIN takes no output format or options of hare's")
        return f"EXPLAIN {sql}"
