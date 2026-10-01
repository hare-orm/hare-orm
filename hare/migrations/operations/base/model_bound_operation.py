from __future__ import annotations

import re

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.migrations.operations.base.hare_operation import HareOperation
from hare.migrations.state.project.state import State
from hare.models import Model
from hare.query.expressions import Q


class ModelBoundOperation(HareOperation):
    """Mixin for an operation whose database_forward/database_backward act on one named
    model - `self.model_name` - resolved from whichever State the direction requires."""

    model_name: str

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.model_name,)

    def _model(self, state: State, app_label: str) -> type[Model]:
        return state.apps.get_model(f"{app_label}.{self.model_name}")

    @staticmethod
    def _references_field_name(sql: Q | RawSQLTerm | str, field_name: str) -> bool:
        """Whether a condition or raw SQL names a field - a ``Q`` by the fields it reads, raw SQL
        (a ``RawSQLTerm``, a trigger's text) by a whole-identifier match, as it has no structured
        field list.

        Args:
            sql: The condition or raw SQL.
            field_name: The model field name (== the DB column name) to look for.

        Returns:
            True if ``field_name`` is named.
        """
        if isinstance(sql, Q):
            return field_name in sql.get_referenced_field_names()
        text = sql.sql if isinstance(sql, RawSQLTerm) else sql
        return re.search(rf"\b{re.escape(field_name)}\b", text) is not None
