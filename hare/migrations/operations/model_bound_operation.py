from __future__ import annotations

import re
from typing import Any

from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.conditions.tenant_condition import TenantCondition
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.models import Model
from hare.query.expressions import Q


class ModelBoundOperation(HareOperation):
    """Mixin for an operation whose database_forward/database_backward act on one named
    model - `self.model_name` - resolved from whichever State the direction requires."""

    model_name: str

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.model_name,)

    def reaches_other_models(self) -> bool:
        """Whether the operation may read or change models other than its own beyond its field -
        raw SQL running against any table. False by default.
        """
        return False

    def get_fields(self) -> list[Any]:
        """The fields the operation declares - none by default."""
        return []

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """Its own model and the models its fields relate to - any model, when it may reach others."""
        if self.reaches_other_models():
            return None
        return self.get_models_labels(app_label, [self.model_name], self.get_fields())

    def get_models_to_reload(self, app_label: str, state: State, field: Any) -> set[tuple[str, str]]:
        """The models a change of one of this model's fields reloads - the model, and the model a
        foreign key or one-to-one field points at.

        Args:
            app_label: The app of the model.
            state: The state.
            field: The changed field.

        Returns:
            The ``(app label, model name)`` of each.
        """
        models_to_reload = {(app_label, self.model_name)}
        if isinstance(field, DIRECT_RELATION_FIELDS):
            models_to_reload.add(state.apps.split_reference(field.model_name))
        return models_to_reload

    def _model(self, state: State, app_label: str) -> type[Model]:
        return state.apps.get_model(f"{app_label}.{self.model_name}")

    @staticmethod
    def _references_field_name(
        sql: Q | RawSQLTerm | ExclusiveArcCondition | TenantCondition | str, field_name: str
    ) -> bool:
        """Whether a condition or raw SQL names a field - a ``Q`` by the fields it reads, raw SQL
        (a ``RawSQLTerm``, a trigger's text) by a whole-identifier match, as it has no structured
        field list.

        Args:
            sql: The condition or raw SQL.
            field_name: The model field name (== the DB column name) to look for.

        Returns:
            True if ``field_name`` is named.
        """
        if isinstance(sql, (Q, ExclusiveArcCondition, TenantCondition)):
            return field_name in sql.get_referenced_field_names()
        text = sql.sql if isinstance(sql, RawSQLTerm) else sql
        return re.search(rf"\b{re.escape(field_name)}\b", text) is not None
