from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ConfigurationError
from hare.fields import Field
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.declared_uniqueness_checks import DeclaredUniquenessChecks
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.fields.alter_field import AlterField
from hare.migrations.operations.fields.field_add_remove_operation import FieldAddRemoveOperation
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.fields.rename_field import RenameField
from hare.migrations.operations.operation import Operation
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AddField(FieldAddRemoveOperation):
    """Adds a field to a model.

    Args:
        model_name: The model.
        name: The field's name.
        field: The field.
        not_valid: For a ``ForeignKeyField``/``OneToOneField`` with a database constraint: the
            existing rows needn't satisfy the FOREIGN KEY yet (where
            ``Features.supports_not_valid_constraints``) - adding it doesn't scan the table while both
            tables' writes wait; a later ``ValidateConstraint`` checks the rows. Another database checks
            them as it adds the column, or never.

    Raises:
        ConfigurationError: ``not_valid`` for a field that makes no FOREIGN KEY.
    """

    def __init__(self, model_name: str, name: str, field: FieldLike, *, not_valid: bool = False) -> None:
        if not_valid and not (isinstance(field, DIRECT_RELATION_FIELDS) and getattr(field, "db_constraint", False)):
            raise ConfigurationError(
                "not_valid=True takes a ForeignKeyField or OneToOneField with db_constraint=True, "
                f"got {type(field).__name__}"
            )
        self.model_name = model_name
        self.name = name
        self.field = field
        self.not_valid = not_valid

    def get_fields(self) -> list[Any]:
        """The added field."""
        return [self.field]

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """A later change of the same field folds into the addition - a removal cancels it."""
        if isinstance(other, (AlterField, RemoveField, RenameField)) and self.is_same_model_name(
            self.model_name, other.model_name
        ):
            if isinstance(other, AlterField) and other.name == self.name:
                keeps_not_valid = (
                    self.not_valid
                    and isinstance(other.field, DIRECT_RELATION_FIELDS)
                    and getattr(other.field, "db_constraint", False)
                )
                return [AddField(self.model_name, self.name, other.field, not_valid=keeps_not_valid)]
            if isinstance(other, RemoveField) and other.name == self.name:
                return []
            if isinstance(other, RenameField) and other.old_name == self.name:
                return [AddField(self.model_name, other.new_name, self.field, not_valid=self.not_valid)]
        return super().reduce(other, app_label)

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_field(app_label, self.model_name, self.name, DependencyType.CREATED)]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """The model and fields the relation needs, and the field's ENUM type."""
        dependencies = RelationDependencies.get_required_for_relation(app_label, self.model_name, self.field)
        enum_type = getattr(self.field, "requires_enum_type", None)
        if enum_type is not None:
            dependencies.append(
                OperationDependency.of_enum_type(enum_type.schema, enum_type.name, DependencyType.CREATED)
            )
        return dependencies

    def describe(self) -> str:
        if self.not_valid:
            return f"Add field {self.name} to {self.model_name}, its foreign key not validated"
        return f"Add field {self.name} to {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.name in model_state.fields:
            raise IncompatibleStateError(f"Field {self.name} already present on model {app_label}.{self.model_name}")

        model_state.set_field(self.name, cast("Field[Any]", deepcopy(self.field)))
        state.mark_models_changed(self.get_changed_model_keys(app_label, self.field))

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
        await self._add_field_to_db(
            old_state, new_state, app_label, state_editor, foreign_key_not_valid=self.not_valid
        )

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        self.raise_if_table_options_unsupported(app_label, new_state, schema_editor)
        model = self._model(new_state, app_label)
        DeclaredUniquenessChecks.raise_if_unsupported(
            schema_editor, model, DeclaredUniquenessChecks.get_unique_field_declarations(model, [self.name])
        )

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        # Going back, old_state holds the field, new_state doesn't.
        await self._remove_field_from_db(old_state, new_state, app_label, state_editor)
