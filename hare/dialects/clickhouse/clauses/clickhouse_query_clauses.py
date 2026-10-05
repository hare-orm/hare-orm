from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.dialects.clickhouse.clauses.constants import (
    CLICKHOUSE_BULK_LOAD_STATEMENT_TEMPLATE,
    CLICKHOUSE_JOINING_SUBQUERY_SETTINGS,
    CLICKHOUSE_SET_OPERATION_SQL,
    CLICKHOUSE_UNBOUNDED_LIMIT_SQL,
    CLICKHOUSE_UPDATE_CONDITION_TEMPLATE,
    CLICKHOUSE_WHERE_PREFIX,
)
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_ROW_COUNT_SQL
from hare.dialects.clickhouse.enums import ClickhouseClause
from hare.dialects.clickhouse.query.clickhouse_sql_context import ClickhouseSqlContext
from hare.exceptions import QueryError, UnSupportedError
from hare.query.enums import TableSampleMethod

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
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

    def get_bulk_load_statement_sql(self, table: str, columns: Sequence[str]) -> str:
        return CLICKHOUSE_BULK_LOAD_STATEMENT_TEMPLATE.format(table=table, columns=", ".join(columns))

    def get_statement_context(self, builder: QueryBuilder, sql_context: SqlContext) -> SqlContext:
        # The aggregates of a grouped statement read at least one row each - their renderer asks.
        if isinstance(sql_context, ClickhouseSqlContext):
            return sql_context.copy(rows_are_grouped=bool(builder._groupbys))
        return sql_context

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

    def get_prewhere_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        criterion = builder._dialect_clauses.get(ClickhouseClause.PREWHERE)
        if criterion is None:
            return ""
        return f" PREWHERE {criterion.get_sql(sql_context.copy(subquery=True))}"

    def get_limit_by_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        limit_by = builder._dialect_clauses.get(ClickhouseClause.LIMIT_BY)
        if limit_by is None:
            return ""
        limit, offset, terms = limit_by
        terms_context = sql_context.copy(with_alias=False, subquery=True)
        offset_sql = f" OFFSET {offset}" if offset else ""
        return f" LIMIT {limit}{offset_sql} BY {', '.join(term.get_sql(terms_context) for term in terms)}"

    def get_update_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``ALTER TABLE t UPDATE column = value, ... WHERE ...`` - every row for no condition -
        followed by the count of the rows it changes (``get_counted_write_sql()``).

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The statement.

        Raises:
            UnSupportedError: The update joins or reads other tables, orders or limits its rows, or
                has a ``WITH`` clause.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering

        if builder._joins or builder._from or builder._orderbys or builder._limit is not None or builder._with:
            raise UnSupportedError(
                "A ClickHouse UPDATE mutation changes the rows of one table matching a condition - it joins, "
                "orders, limits and reads a WITH clause of nothing"
            )
        field_context = sql_context.copy(with_namespace=False)
        value_context = sql_context.copy(subquery=True)
        assignments_sql = ", ".join(
            f"{field.get_sql(field_context)} = {value.get_sql(value_context)}" for field, value in builder._updates
        )
        where_sql = " WHERE 1"
        if builder._wheres:
            # ClickHouse 25.8 refuses an UPDATE of a constant value whose condition, a negation, folds into
            # a constant (NOT of a non-Nullable column's IS NULL) - the condition is no negation so.
            condition_sql = QuerySqlRendering.where_sql(builder, sql_context).removeprefix(CLICKHOUSE_WHERE_PREFIX)
            where_sql = CLICKHOUSE_UPDATE_CONDITION_TEMPLATE.format(condition=condition_sql)
        table_sql = builder._update_table.get_sql(sql_context)  # type: ignore[union-attr]
        update_sql = self.get_update_head_sql(builder._update_table, table_sql)
        return self.get_counted_write_sql(
            f"{update_sql} {assignments_sql}{where_sql}{self.get_statement_end_sql(builder, sql_context)}",
            table_sql,
            where_sql,
        )

    def get_delete_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``DELETE FROM t WHERE ...`` - every row for no condition - followed by the count of the
        rows it removes (``get_counted_write_sql()``).

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The statement.

        Raises:
            UnSupportedError: The delete joins other tables, orders or limits its rows, or has a
                ``WITH`` clause.
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
        where_sql = QuerySqlRendering.where_sql(builder, sql_context) if builder._wheres else " WHERE 1"
        table_sql = builder._from[0].get_sql(sql_context)
        statement_end_sql = self.get_statement_end_sql(builder, sql_context)
        # The table and the condition are hare's own rendering; values are inlined literals.
        if self.deletes_by_mutation(builder._from[0]):
            delete_sql = f"ALTER TABLE {table_sql} DELETE{where_sql}{statement_end_sql}"
        else:
            delete_sql = f"DELETE FROM {table_sql}{where_sql}{statement_end_sql}"  # nosec B608
        return self.get_counted_write_sql(delete_sql, table_sql, where_sql)

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

    def get_update_head_sql(self, table: Any, table_sql: str) -> str:
        """How an update of a table begins - a lightweight ``UPDATE`` of a table declaring
        ``lightweight_updates`` on a server that has them, else an ``ALTER TABLE ... UPDATE`` mutation.

        Args:
            table: The updated table.
            table_sql: Its SQL.

        Returns:
            The words before the assignments.
        """
        # A connection to a server without lightweight UPDATE refuses such a model when it is bound.
        if self.get_model_table_options(table, self.dialect).lightweight_updates:
            return f"UPDATE {table_sql} SET"
        return f"ALTER TABLE {table_sql} UPDATE"

    def deletes_by_mutation(self, table: Any) -> bool:
        """Whether the rows of a table are deleted by an ``ALTER TABLE ... DELETE`` mutation - a table
        with projections on a server whose lightweight ``DELETE`` refuses one.

        Args:
            table: The table.

        Returns:
            Whether they are.
        """
        if not self.get_model_table_options(table, self.dialect).projections:
            return False
        return not cast("ClickhouseDialect", self.dialect).rebuilds_projections

    @staticmethod
    def get_counted_write_sql(write_sql: str, table_sql: str, where_sql: str) -> str:
        """A write followed by the count of the rows it matches - ClickHouse reports no rows changed
        by a mutation, so the client runs the count first, then the write. The write comes first in
        the text: its values keep their numbers, and the count's condition reuses them.

        Args:
            write_sql: The UPDATE or DELETE.
            table_sql: The written table.
            where_sql: The write's ``WHERE`` clause.

        Returns:
            The two statements.
        """
        return f"{write_sql}; {CLICKHOUSE_ROW_COUNT_SQL}{table_sql}{where_sql}"

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
        are an array of tuples, a column's new value the matching tuple's element - followed by the
        count of the rows it changes (``get_counted_write_sql()``).

        Args:
            with_sql: The ``WITH`` clause, or an empty string.
            table: The updated table.
            table_sql: Its SQL.
            assignments: ``(column, value SQL)`` per assigned column - the value names a row column.
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
        assignments_sql = ", ".join(
            f"{column} = tupleElement(arrayElement({rows_sql}, {position_sql}), {get_row_element_number(value)})"
            for column, value in assignments
        )
        # The rows' keys as a set first, which skips every other table row cheaply - the search of
        # the rows runs for the matching ones alone.
        key_columns_sql = ", ".join(column.removeprefix(table_prefix) for column, _value in matches)
        row_keys_sql = ", ".join(get_row_element_sql(value) for _column, value in matches)
        key_set_sql = f"(SELECT arrayJoin(arrayMap(row -> tuple({row_keys_sql}), {rows_sql})))"
        where_sql = f" WHERE tuple({key_columns_sql}) IN {key_set_sql} AND {position_sql} > 0"
        if condition_sql:
            where_sql += f" AND ({condition_sql.replace(table_prefix, '')})"
        return self.get_counted_write_sql(
            f"{self.get_update_head_sql(table, table_sql)} {assignments_sql}{where_sql}", table_sql, where_sql
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
