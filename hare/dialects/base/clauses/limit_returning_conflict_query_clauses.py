from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.sql.exceptions import QueryException

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.sql_context import SqlContext
    from hare.sql.tables.table import Table


class LimitReturningConflictQueryClauses(QueryClauses):
    """The clauses of the databases that page with ``LIMIT``/``OFFSET``, read written rows back with
    ``RETURNING`` and resolve a conflicting insert with ``ON CONFLICT`` - with them, an ``UPDATE``
    reading other tables in ``FROM`` and one from a ``VALUES`` table. None of it is ISO SQL; several
    databases write it alike, and the dialect of each extends this class.

    Attributes:
        unbounded_limit_sql: The ``LIMIT`` an ``OFFSET`` needs when no limit is set, None when it
            needs none.
    """

    unbounded_limit_sql: ClassVar[str | None] = None
    #: The alias of the ``VALUES`` rows inside the table an ``UPDATE`` reads them from - PostgreSQL
    #: before 16 refuses a subquery in ``FROM`` without one.
    values_rows_alias: ClassVar[str] = "hare_values_rows"

    def get_limit_offset_sql(self, limit_sql: str | None, offset_sql: str | None) -> str:
        if limit_sql is None:
            limit_sql = self.unbounded_limit_sql if offset_sql is not None else None
        sql = f" LIMIT {limit_sql}" if limit_sql is not None else ""
        if offset_sql is not None:
            sql += f" OFFSET {offset_sql}"
        return sql

    def get_returning_sql(self, returned: Sequence[ReturnedValue]) -> str:
        if not returned:
            return ""
        return " RETURNING " + ",".join(self.get_returned_value_sql(value) for value in returned)

    def get_returned_value_sql(self, value: ReturnedValue) -> str:
        """One value of a ``RETURNING`` clause.

        Args:
            value: The value.

        Returns:
            Its expression, under its alias when it has one.
        """
        return f"{value.sql} AS {value.alias_sql}" if value.alias_sql is not None else value.sql

    def get_on_conflict_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        if not builder._on_conflict:
            return ""
        return self.get_conflict_target_sql(builder, sql_context) + self.get_conflict_action_sql(builder, sql_context)

    @staticmethod
    def get_conflict_target_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``ON CONFLICT`` and the constraint or the columns a conflict is found on.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause.

        Raises:
            QueryException: The conflict has no handler, or a handler and a target that don't fit.
        """
        if not builder._on_conflict_do_nothing and len(builder._on_conflict_do_updates) == 0:
            if not builder._on_conflict_fields and not builder._on_conflict_constraint:
                return ""
            raise QueryException("No handler defined for on conflict")

        if builder._on_conflict_do_updates and not builder._on_conflict_fields and not builder._on_conflict_constraint:
            raise QueryException("Can not have fieldless on conflict do update")

        conflict_query = " ON CONFLICT"
        if builder._on_conflict_constraint:
            conflict_query += f" ON CONSTRAINT {sql_context.quote(builder._on_conflict_constraint)}"
        elif builder._on_conflict_fields:
            on_conflict_context = sql_context.copy(with_alias=True)
            fields = [
                field.get_sql(on_conflict_context)  # type:ignore[union-attr]
                for field in builder._on_conflict_fields
            ]
            conflict_query += " (" + ", ".join(fields) + ")"

        if builder._on_conflict_wheres:
            if builder._on_conflict_constraint:
                raise QueryException("Can not use a WHERE index predicate with ON CONSTRAINT")
            where_context = sql_context.copy(subquery=True)
            conflict_query += f" WHERE {builder._on_conflict_wheres.get_sql(where_context)}"

        return conflict_query

    @staticmethod
    def get_conflict_action_sql(builder: QueryBuilder, sql_context: SqlContext) -> str:
        """``DO NOTHING`` or ``DO UPDATE SET ...`` of a conflict.

        Args:
            builder: The query.
            sql_context: The context it renders in.

        Returns:
            The clause.
        """
        sql_context = sql_context.copy(with_namespace=False)
        if builder._on_conflict_do_nothing:
            return " DO NOTHING"
        if not builder._on_conflict_do_updates:
            return ""
        updates = []
        value_context = sql_context.copy(with_namespace=True)
        for field, value in builder._on_conflict_do_updates:
            if value:
                updates.append(f"{field.get_sql(sql_context)}={value.get_sql(value_context)}")
            else:
                updates.append(f"{field.get_sql(sql_context)}=EXCLUDED.{field.get_sql(sql_context)}")
        action_sql = " DO UPDATE SET {updates}".format(updates=",".join(updates))  # nosec:B608
        if builder._on_conflict_do_update_wheres:
            action_sql += " WHERE {where}".format(
                where=builder._on_conflict_do_update_wheres.get_sql(
                    sql_context.copy(subquery=True, with_namespace=True)
                )
            )
        return action_sql

    def get_update_sql(self, builder: QueryBuilder, sql_context: SqlContext) -> str:
        # The other tables are read in FROM; with JOINs, the updated table is read there too under
        # an alias of its own, which the JOINs then refer to.
        # Local import: the SQL builder package imports the dialects, which load this module.
        from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering

        querystring = QuerySqlRendering.with_sql(builder._with, sql_context) if builder._with else ""
        querystring += QuerySqlRendering.update_sql(builder, sql_context)
        querystring += QuerySqlRendering.set_sql(builder, sql_context)

        # A local list - get_sql() may run several times and must not change the builder.
        from_selectables = list(builder._from)
        update_table = builder._update_table
        if builder._joins and update_table is not None:
            from_selectables.append(update_table.as_(update_table.get_table_name() + "_"))

        if from_selectables:
            from_context = sql_context.copy(subquery=True, with_alias=True)
            querystring += " FROM {selectable}".format(
                selectable=",".join(clause.get_sql(from_context) for clause in from_selectables)
            )
        if builder._joins:
            querystring += " " + " ".join(join.get_sql(sql_context) for join in builder._joins)

        if builder._wheres:
            querystring += QuerySqlRendering.where_sql(builder, sql_context)

        if builder._orderbys:
            querystring += builder._orderby_sql(sql_context)
        if builder._limit is not None:
            querystring += self.get_limit_offset_sql(builder._limit.get_sql(sql_context), None)
        return querystring

    def get_values_table_columns_sql(self, column_names: Sequence[str]) -> str:
        # A VALUES table's columns are column1, column2, ... in both databases.
        return ", ".join(f"column{index} AS {name}" for index, name in enumerate(column_names, start=1))

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
        set_sql = ", ".join(f"{column} = {value_sql}" for column, value_sql in assignments)
        where_parts = [f"{column_sql} = {value_sql}" for column_sql, value_sql in matches]
        if condition_sql is not None:
            where_parts.append(f"({condition_sql})")
        values_rows_sql = f"(VALUES {values_sql}) AS {self.dialect.literals.quote_identifier(self.values_rows_alias)}"
        return (
            f"{with_sql}UPDATE {table_sql} SET {set_sql} "  # nosec B608
            f"FROM (SELECT {values_columns_sql} FROM {values_rows_sql}) AS {values_alias_sql} "
            f"WHERE {' AND '.join(where_parts)}"
        ) + self.get_returning_sql(returned)
