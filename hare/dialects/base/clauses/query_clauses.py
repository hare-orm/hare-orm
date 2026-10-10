from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from hare.dialects.base.clauses.enums import MergeAction, MergeMatch
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.clauses.merge_when_sql import MergeWhenSql
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.enums import SetOperation
    from hare.sql.sql_context import SqlContext
    from hare.sql.tables.table import Table


class QueryClauses:
    """How a dialect writes the clauses of a statement whose SQL differs between databases -
    ``DISTINCT ON``, row locks, ``LIMIT``/``OFFSET``, ``RETURNING``, ``ON CONFLICT``, ``UPDATE ...
    FROM`` - and the statements hare writes as text (``EXPLAIN``, an ``UPDATE`` from a ``VALUES``
    table). This base writes ISO SQL; a clause ISO SQL has no form for raises ``UnSupportedError``
    when a statement needs it.
    """

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose statements are written.
        """
        self.dialect = dialect

    def get_statement_context(self, builder: QueryBuilder, sql_context: SqlContext) -> SqlContext:
        """The context a statement's own terms render in - a dialect whose renderers need to know
        something of the statement around a term puts it into a context of its own here.

        Args:
            builder: The statement.
            sql_context: The context the statement is rendered in.

        Returns:
            The same context by default.
        """
        return sql_context

    def get_bulk_load_statement_sql(self, table: str, columns: Sequence[str]) -> str:
        """The statement a bulk load of rows (``DatabaseClient.copy()``) is reported under to the
        observers - the rows travel in no SQL text.

        Args:
            table: The table loaded into.
            columns: Its loaded columns.

        Returns:
            The statement.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def get_distinct_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The ``DISTINCT`` of a ``SELECT``, with its trailing space.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause, ``""`` for none.

        Raises:
            UnSupportedError: The query has ``distinct_on()``.
        """
        if builder._distinct_on:
            raise UnSupportedError(f"distinct_on() has no SQL for the {self.dialect} dialect")
        return "DISTINCT " if builder._distinct else ""

    def get_statement_end_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """What a subquery, or a query with clauses of the dialect's QuerySet methods
        (``QueryBuilder._dialect_clauses``), ends with after its ``LIMIT`` - with its leading space.

        Args:
            builder: The query.
            sql_context: The context it renders in - ``sql_context.subquery`` for a subquery.

        Returns:
            The text, ``""`` for none.
        """
        return ""

    def get_main_table_suffix_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """What follows the first table of ``FROM`` - its sample (``sample()``,
        ``get_table_sample_sql()``) by default; a dialect adds what its QuerySet methods put there.
        Asked when the query reads a sample or has clauses of the dialect's QuerySet methods.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The text with its leading space, ``""`` for none.
        """
        # Local import: the SQL terms import the dialects, which load this module.
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        if builder._table_sample is None:
            return ""
        method, percent, seed = builder._table_sample
        return self.get_table_sample_sql(
            method,
            ValueWrapper(percent, allow_parametrize=False).get_sql(sql_context),
            None if seed is None else ValueWrapper(seed, allow_parametrize=False).get_sql(sql_context),
        )

    def get_prewhere_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The condition read before ``WHERE`` - after the JOINs. Asked only of a query with clauses
        of the dialect's QuerySet methods.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause with its leading space, ``""`` for none.
        """
        return ""

    def get_limit_by_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The limit of rows per group of values - after ``ORDER BY``, before ``LIMIT``. Asked only
        of a query with clauses of the dialect's QuerySet methods.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause with its leading space, ``""`` for none.
        """
        return ""

    def get_row_lock_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The row lock a ``SELECT`` takes (``select_for_update()``), with its leading space.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause, ``""`` for none.

        Raises:
            UnSupportedError: The query locks rows - ISO SQL's ``SELECT`` has no lock clause.
        """
        if builder._for_update:
            raise UnSupportedError(f"select_for_update() has no SQL for the {self.dialect} dialect")
        return ""

    def get_limit_offset_sql(self, limit_sql: str | None, offset_sql: str | None) -> str:
        """The clauses bounding a query's rows, with their leading space.

        Args:
            limit_sql: The SQL of the most rows, None for no bound.
            offset_sql: The SQL of the rows skipped, None for none.

        Returns:
            ``OFFSET ... ROWS FETCH FIRST ... ROWS ONLY`` - ISO SQL's form.
        """
        sql = f" OFFSET {offset_sql} ROWS" if offset_sql is not None else ""
        if limit_sql is not None:
            sql += f" FETCH FIRST {limit_sql} ROWS ONLY"
        return sql

    def get_returning_sql(self, returned: Sequence[ReturnedValue]) -> str:
        """The clause returning a written row's values, with its leading space.

        Args:
            returned: The returned values.

        Returns:
            The clause, ``""`` when nothing is returned.

        Raises:
            UnSupportedError: Values are returned - ISO SQL has no ``RETURNING``.
        """
        if returned:
            raise UnSupportedError(f"RETURNING has no SQL for the {self.dialect} dialect")
        return ""

    def get_old_row_value_sql(self, column_sql: str) -> str:
        """A column of a written row as it was before the write, in its ``RETURNING``.

        Args:
            column_sql: The quoted column.

        Returns:
            The SQL.

        Raises:
            UnSupportedError: Always - ISO SQL's ``RETURNING`` reads the row as written only.
        """
        raise UnSupportedError(f"RETURNING of the row before the write has no SQL for the {self.dialect} dialect")

    def get_json_table_sql(
        self,
        document_sql: str,
        path_sql: str,
        columns_sql: Sequence[tuple[str, str, str]],
        ordinality_name_sql: str | None,
    ) -> str:
        """The rows a JSON document's items make, as a table in ``FROM``.

        Args:
            document_sql: The document.
            path_sql: The JSON path of the items, as a literal.
            columns_sql: Each column's quoted name, type and path literal.
            ordinality_name_sql: The quoted column numbering the items, None for none.

        Returns:
            ``JSON_TABLE(... COLUMNS (...))`` - ISO SQL's form.
        """
        columns = [
            f"{name_sql} {type_sql} PATH {column_path_sql}" for name_sql, type_sql, column_path_sql in columns_sql
        ]
        if ordinality_name_sql is not None:
            columns.insert(0, f"{ordinality_name_sql} FOR ORDINALITY")
        return f"JSON_TABLE({document_sql}, {path_sql} COLUMNS ({', '.join(columns)}))"

    def get_lateral_sql(self, subquery_sql: str) -> str:
        """A subquery joined in ``FROM`` that reads the columns of the tables before it.

        Args:
            subquery_sql: The subquery, in parentheses.

        Returns:
            ``LATERAL (...)`` - ISO SQL's form.
        """
        return f"LATERAL {subquery_sql}"

    def get_table_sample_sql(self, method: str, percent_sql: str, seed_sql: str | None) -> str:
        """The sample a table in ``FROM`` is read in (``sample()``), with its leading space.

        Args:
            method: How the rows are picked - ``BERNOULLI`` or ``SYSTEM``.
            percent_sql: The chance of each row or block, in percent.
            seed_sql: The seed repeating the same sample; None for a new one each run.

        Returns:
            ``TABLESAMPLE <method> (<percent>) [REPEATABLE (<seed>)]`` - ISO SQL's form.
        """
        sql = f" TABLESAMPLE {method} ({percent_sql})"
        if seed_sql is not None:
            sql += f" REPEATABLE ({seed_sql})"
        return sql

    def get_merge_sql(
        self,
        *,
        target_sql: str,
        source_sql: str,
        source_alias_sql: str,
        on_sql: str,
        whens: Sequence[MergeWhenSql],
        returned: Sequence[ReturnedValue],
        action_alias_sql: str,
    ) -> str:
        """A ``MERGE`` statement.

        Args:
            target_sql: The written table.
            source_sql: The source rows - a subquery in parentheses.
            source_alias_sql: The quoted name the source is read under.
            on_sql: The condition matching a source row to a target row.
            whens: The ``WHEN`` branches, in order.
            returned: The values each written row returns, none for no ``RETURNING``.
            action_alias_sql: The quoted name the action that wrote a row is returned under, first.

        Returns:
            The statement.
        """
        whens_sql = "".join(self.get_merge_when_sql(when) for when in whens)
        return (
            f"MERGE INTO {target_sql} USING {source_sql} AS {source_alias_sql} ON {on_sql}{whens_sql}"
            f"{self.get_merge_returning_sql(returned, action_alias_sql)}"
        )

    def get_merge_when_sql(self, when: MergeWhenSql) -> str:
        """One ``WHEN`` branch of a ``MERGE``, with its leading space.

        Args:
            when: The branch.

        Returns:
            The branch.

        Raises:
            UnSupportedError: The branch takes the target rows no source row matches, or does
                nothing - ISO SQL's ``MERGE`` has neither.
        """
        if when.match is MergeMatch.NOT_MATCHED_BY_SOURCE or when.action is MergeAction.DO_NOTHING:
            raise UnSupportedError(
                f"A MERGE branch {when.match} doing {when.action} has no SQL for the {self.dialect} dialect"
            )
        match_sql = "MATCHED" if when.match is MergeMatch.MATCHED else "NOT MATCHED"
        return f" WHEN {match_sql}{self.get_merge_condition_sql(when)} THEN {self.get_merge_action_sql(when)}"

    @staticmethod
    def get_merge_condition_sql(when: MergeWhenSql) -> str:
        """The ``AND`` condition of a ``WHEN`` branch, with its leading space; ``""`` for none."""
        return "" if when.condition_sql is None else f" AND {when.condition_sql}"

    @staticmethod
    def get_merge_action_sql(when: MergeWhenSql) -> str:
        """What a ``WHEN`` branch does - ``UPDATE SET ...``, ``DELETE``, ``INSERT (...) VALUES (...)``.

        Args:
            when: The branch.

        Returns:
            The action.
        """
        if when.action is MergeAction.UPDATE:
            assignments = ", ".join(
                f"{column} = {value}" for column, value in zip(when.columns_sql, when.values_sql, strict=True)
            )
            return f"UPDATE SET {assignments}"  # nosec B608
        if when.action is MergeAction.DELETE:
            return "DELETE"
        return f"INSERT ({', '.join(when.columns_sql)}) VALUES ({', '.join(when.values_sql)})"

    def get_merge_returning_sql(self, returned: Sequence[ReturnedValue], action_alias_sql: str) -> str:
        """The ``RETURNING`` of a ``MERGE``, with its leading space - the action that wrote the row,
        then the values.

        Args:
            returned: The values, none for no ``RETURNING``.
            action_alias_sql: The quoted name the action that wrote the row is returned under.

        Returns:
            The clause, ``""`` when nothing is returned.

        Raises:
            UnSupportedError: Values are returned - ISO SQL's ``MERGE`` has no ``RETURNING``.
        """
        if returned:
            raise UnSupportedError(f"MERGE ... RETURNING has no SQL for the {self.dialect} dialect")
        return ""

    def get_on_conflict_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """The conflict handling of an ``INSERT`` (``on_conflict()``), with its leading space.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause, ``""`` for none.

        Raises:
            UnSupportedError: The query handles conflicts - ISO SQL's ``INSERT`` has no such clause.
        """
        if builder._on_conflict:
            raise UnSupportedError(f"ON CONFLICT has no SQL for the {self.dialect} dialect")
        return ""

    def get_set_operation_sql(self, set_operation: SetOperation) -> str:
        """The keyword joining two queries of a set operation.

        Args:
            set_operation: The operation.

        Returns:
            ISO SQL's keyword - ``UNION``, ``INTERSECT`` and ``EXCEPT`` drop repeated rows.
        """
        return set_operation.value

    def get_like_escape_sql(self, escape_sql: str) -> str:
        """The ``ESCAPE`` clause of a ``LIKE`` pattern.

        Args:
            escape_sql: The clause, with its leading space - a backslash as the escape character for
                hare's own lookups.

        Returns:
            The clause as the database writes it - ISO SQL's as given.
        """
        return escape_sql

    def get_delete_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """A ``DELETE`` statement, without ``RETURNING`` - ``DELETE FROM`` the table, its joins and
        conditions, and the order and limit where ``Features.supports_update_limit_order_by``.

        Args:
            builder: The query.
            sql_context: The context it renders in, its namespace already set.

        Returns:
            The statement.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering

        return QuerySqlRendering.select_insert_delete_sql(builder, sql_context)

    def get_update_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        """An ``UPDATE`` statement, without ``RETURNING``.

        Args:
            builder: The query.
            sql_context: The context it renders in, its namespace already set.

        Returns:
            The statement.

        Raises:
            UnSupportedError: The update joins or reads other tables, orders or limits its rows -
                ISO SQL's ``UPDATE`` does none of these.
        """
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering

        if builder._joins or builder._from or builder._orderbys or builder._limit is not None:
            raise UnSupportedError(f"An UPDATE joining other tables has no SQL for the {self.dialect} dialect")
        querystring = QuerySqlRendering.with_sql(builder._with, sql_context) if builder._with else ""
        querystring += QuerySqlRendering.update_sql(builder, sql_context)
        querystring += QuerySqlRendering.set_sql(builder, sql_context)
        if builder._wheres:
            querystring += QuerySqlRendering.where_sql(builder, sql_context)
        return querystring

    def get_typed_placeholder_template(self, placeholder_template: str, cast_type: str | None) -> str:
        """A parameter placeholder's template, cast to a type when one is given.

        Args:
            placeholder_template: The dialect's placeholder template.
            cast_type: The type to cast the parameter to, None for none.

        Returns:
            The template.
        """
        if cast_type is None:
            return placeholder_template
        return f"CAST({placeholder_template} AS {cast_type.upper()})"

    def get_insert_rows_source_sql(
        self, table_sql: str, rows_source_sql: str, returned: Sequence[ReturnedValue]
    ) -> str:
        """An ``INSERT`` of the rows a source gives - the dialect's rows of column defaults.

        Args:
            table_sql: The table inserted into.
            rows_source_sql: The source of the rows.
            returned: The returned values, as ``get_returning_sql()`` takes them.

        Returns:
            The statement.
        """
        return f"INSERT INTO {table_sql} {rows_source_sql}" + self.get_returning_sql(returned)  # nosec B608

    def get_values_table_columns_sql(self, column_names: Sequence[str]) -> str:
        """The select list giving the columns of a ``VALUES`` table their names.

        Args:
            column_names: The names, in column order.

        Returns:
            The select list.

        Raises:
            UnSupportedError: A ``VALUES`` table's columns have no names the dialect reads them by.
        """
        raise UnSupportedError(f"An UPDATE from a VALUES table has no SQL for the {self.dialect} dialect")

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
        """An ``UPDATE`` writing each row of a ``VALUES`` table into the table row it matches.

        Args:
            with_sql: The ``WITH`` clause, or an empty string.
            table: The updated table.
            table_sql: Its SQL.
            assignments: ``(column, value SQL)`` per assigned column.
            values_columns_sql: The select list naming the ``VALUES`` table's columns.
            values_sql: The rows of the ``VALUES`` table.
            values_alias_sql: The alias of the ``VALUES`` table.
            matches: ``(table column SQL, VALUES column SQL)`` pairs a row is matched by.
            condition_sql: A further condition on the updated rows, or None.
            returned: The returned values, as ``get_returning_sql()`` takes them.

        Returns:
            The statement.

        Raises:
            UnSupportedError: ISO SQL's ``UPDATE`` reads no other table.
        """
        raise UnSupportedError(f"An UPDATE from a VALUES table has no SQL for the {self.dialect} dialect")

    def get_upsert_inserted_flag_sql(self) -> str | None:
        """A boolean expression a ``RETURNING`` row of ``INSERT ... ON CONFLICT DO UPDATE`` holds
        - true for a row the statement inserted, false for one it updated.

        Returns:
            The expression, None when the database has none - an upsert then reads which rows
            exist before it writes, when a listener needs to know.
        """
        return None

    def get_explain_sql(
        self, sql: str, output_format: str | None, options: Mapping[str, bool], features: Features
    ) -> str:
        """The statement showing the plan of ``sql``.

        Args:
            sql: The explained statement.
            output_format: The plan's output format, None for the dialect's default.
            options: The EXPLAIN options, each turned on or off.
            features: The connection's features - an option may need a newer server.

        Returns:
            The EXPLAIN statement.

        Raises:
            UnSupportedError: The dialect has no such statement, format or option.
        """
        raise UnSupportedError(f"EXPLAIN has no SQL for the {self.dialect} dialect")
