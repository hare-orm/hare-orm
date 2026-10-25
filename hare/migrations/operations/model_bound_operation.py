from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.field_condition import FieldCondition
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


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

    def get_changed_model_keys(self, app_label: str, field: Any) -> set[tuple[str, str]]:
        """The models a change of one of this model's fields changes - the model, and the model a
        foreign key or one-to-one field points at.

        Args:
            app_label: The app of the model.
            field: The changed field.

        Returns:
            The ``(app label, model name)`` of each.
        """
        changed_model_keys = {(app_label, self.model_name)}
        if isinstance(field, DIRECT_RELATION_FIELDS):
            changed_model_keys.add(StateApps.split_reference(field.model_name))
        return changed_model_keys

    def _model(self, state: State, app_label: str) -> type[Model]:
        return state.apps.get_model(f"{app_label}.{self.model_name}")

    def raise_if_table_options_unsupported(
        self, app_label: str, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        """Refuses a change the model's table options don't allow once it is made - a unique
        constraint, index or field of a partitioned table without its partition key - as creating
        the table with it would.

        Args:
            app_label: The migration's app.
            new_state: The state after the operation.
            schema_editor: The schema editor it runs with.

        Raises:
            ConfigurationError: The model doesn't fit its options.
        """
        model = self._model(new_state, app_label)
        table_options = model._meta.get_table_options(schema_editor.client.dialect)
        if model._meta.managed is not False and table_options is not None:
            table_options.raise_if_unsupported(model, schema_editor.client.features)

    @staticmethod
    def condition_reads_field(condition: Any, field_name: str) -> bool:
        """Whether a condition reads a field - a ``Q`` by the fields it names; raw SQL isn't read.

        Args:
            condition: The condition, None for none.
            field_name: The model field name.

        Returns:
            True when it names the field.
        """
        if isinstance(condition, FieldCondition):
            return field_name in condition.get_referenced_field_names()
        return False
