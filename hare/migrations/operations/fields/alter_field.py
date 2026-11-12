from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.declared_uniqueness_checks import DeclaredUniquenessChecks
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.operations.operation_rewrite import OperationRewrite
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterField(ModelBoundOperation):
    """Changes the definition of a model's field."""

    def __init__(self, model_name: str, name: str, field: FieldLike) -> None:
        self.model_name = model_name
        self.name = name
        self.field = field

    def get_fields(self) -> list[Any]:
        """The field's new definition."""
        return [self.field]

    def get_rewrite(self, other: Operation, app_label: str) -> OperationRewrite | None:
        """A later change or removal of the same field takes this one's place."""
        if (
            isinstance(other, (AlterField, RemoveField))
            and self.is_same_model_name(self.model_name, other.model_name)
            and other.name == self.name
        ):
            return OperationRewrite((other,))
        return super().get_rewrite(other, app_label)

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_field(app_label, self.model_name, self.name, DependencyType.CHANGED)]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """The model and fields the relation needs, and the field's ENUM type."""
        return RelationDependencies.get_required_for_field(app_label, self.model_name, self.field, state)

    def describe(self) -> str:
        return f"Alter field {self.name} on {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.name not in model_state.fields:
            raise IncompatibleStateError(f"Field {self.name} is not present on model {app_label}.{self.model_name}")

        model_state.set_field(self.name, self.field.snapshot())  # type: ignore[union-attr]
        state.mark_models_changed(self.get_changed_model_keys(app_label, self.field, state))

    def get_effect(self, app_label: str, state: State, schema_editor: BaseSchemaEditor) -> OperationEffect:
        new_state = state.clone()
        self.state_forward(app_label, new_state)
        old_model = self._model(state, app_label)
        new_model = self._model(new_state, app_label)
        plan = schema_editor.get_alter_field_plan(old_model, new_model, self.name, new_state.get_models_with_tables())
        rewrites_table = SchemaEffect.REWRITE_ROWS in plan.effects
        loses_data = schema_editor.column_type_changes.changes_column_type(
            old_model, new_model, self.name, schema_editor.client.dialect
        )
        reasons = []
        if loses_data:
            reasons.append(
                f"changes the column type of {self.model_name}.{self.name} - a value the new type can't hold is lost"
            )
        if rewrites_table:
            reasons.append(f"rewrites the table of {self.model_name}")
        return OperationEffect(
            operation=self,
            reversible=self.reversible,
            rewrites_table=rewrites_table,
            loses_data=loses_data,
            reason="; ".join(reasons) or None,
        )

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        model = self._model(new_state, app_label)
        DeclaredUniquenessChecks.raise_if_unsupported(
            schema_editor, model, DeclaredUniquenessChecks.get_unique_field_declarations(model, [self.name])
        )

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        self.raise_if_table_options_unsupported(app_label, new_state, state_editor)
        old_model = self._model(old_state, app_label)
        new_model = self._model(new_state, app_label)
        await state_editor.alter_field(old_model, new_model, self.name, new_state.get_models_with_tables())

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        # Altering back is altering from the later state to the earlier one - the states come swapped.
        await self.database_forward(app_label, old_state, new_state, state_editor)
