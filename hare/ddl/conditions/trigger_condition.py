from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constants import TRIGGER_CONDITION_TABLE_ALIAS
from hare.ddl.raw_sql_term import RawSQLTerm

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.expressions import Q


class TriggerCondition:
    """The SQL a trigger's ``WHEN`` condition is written into DDL as - a ``RawSQLTerm`` as given, a
    ``Q`` over the model's own fields against the trigger's row with its values inline."""

    @staticmethod
    def get_sql(condition: Q | RawSQLTerm, row: str, model: type[Model], client: DatabaseClient) -> str:
        """Renders a trigger's condition.

        Args:
            condition: Raw SQL, or a ``Q`` over the model's own fields.
            row: The row a ``Q`` reads - ``NEW`` or ``OLD``.
            model: The model the trigger is declared on.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate.

        Raises:
            ConfigurationError: The ``Q`` is empty, or reads another model's fields or an aggregate.
        """
        if isinstance(condition, RawSQLTerm):
            return condition.sql
        # Local import: hare.ddl is imported by the field and model modules hare.sql's tables need.
        from hare.sql import Table

        table = Table(model._meta.db_table).as_(TRIGGER_CONDITION_TABLE_ALIAS)
        sql = ConstraintCondition.get_sql(condition, model, client, table=table)
        # Each column is read off the trigger's row, a keyword written as it is.
        quoted_alias = client.query_class.SQL_CONTEXT.quote(TRIGGER_CONDITION_TABLE_ALIAS)
        return sql.replace(f"{quoted_alias}.", f"{row}.")
