from __future__ import annotations

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_CHECK_CONSTRAINT_PREFIX,
    CLICKHOUSE_CONSTRAINT_DEFINITION_PREFIX,
)


class ClickhouseCheckConstraints:
    """The CHECK constraints of a ClickHouse table, read from the column list of its ``CREATE TABLE``
    - the server keeps them nowhere else."""

    @staticmethod
    def get_check_constraints(create_table_sql: str) -> list[CheckConstraint]:
        """The table's CHECK constraints - an ``ASSUME`` constraint, checked by no write, left out.

        Args:
            create_table_sql: The statement, as the server keeps it.

        Returns:
            Each constraint, its condition as the server writes it.
        """
        check_constraints = []
        column_list_sql, _ = ClickhouseSqlParts.get_parenthesised(create_table_sql)
        for definition_sql in ClickhouseSqlParts.split(column_list_sql):
            if not definition_sql.startswith(CLICKHOUSE_CONSTRAINT_DEFINITION_PREFIX):
                continue
            name, condition_sql = ClickhouseSqlParts.get_leading_identifier(
                definition_sql[len(CLICKHOUSE_CONSTRAINT_DEFINITION_PREFIX) :].lstrip()
            )
            condition_sql = condition_sql.lstrip()
            if not name or not condition_sql.startswith(CLICKHOUSE_CHECK_CONSTRAINT_PREFIX):
                continue
            check_sql = condition_sql[len(CLICKHOUSE_CHECK_CONSTRAINT_PREFIX) :].strip()
            check_constraints.append(CheckConstraint(name=name, check=RawSQLTerm(check_sql)))
        return check_constraints
