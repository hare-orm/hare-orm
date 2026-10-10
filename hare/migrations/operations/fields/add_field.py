from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ConfigurationError
from hare.fields import Field
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.fields.alter_field import AlterField
from hare.migrations.operations.fields.field_add_remove_operation import FieldAddRemoveOperation
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.fields.rename_field import RenameField
from hare.migrations.operations.operation import Operation
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AddField(FieldAddRemoveOperation):
    """Adds a field to a model.

    Args:
        model_name: The model.
        name: The field's name.
        field: The field.
        not_valid: For a ``ForeignKeyField``/``OneToOneField`` with a database constraint: the
            existing rows needn't satisfy the FOREIGN KEY yet (Postgres ``NOT VALID``) - adding it
            doesn't scan the table while both tables' writes wait; a later ``ValidateConstraint``
            checks the rows. SQLite never checks them as it adds the column.

    Raises:
        ConfigurationError: ``not_valid`` for a field that makes no FOREIGN KEY.
    """

    reads_old_state = False

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

    def describe(self) -> str:
        if self.not_valid:
            return f"Add field {self.name} to {self.model_name}, its foreign key not validated"
        return f"Add field {self.name} to {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.name in model_state.fields:
            raise IncompatibleStateError(f"Field {self.name} already present on model {app_label}.{self.model_name}")

        model_state.set_field(self.name, cast("Field[Any]", deepcopy(self.field)))
        state.reload_models(self.get_models_to_reload(app_label, state, self.field))

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self._add_field_to_db(new_state, app_label, state_editor, foreign_key_not_valid=self.not_valid)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self._remove_field_from_db(old_state, app_label, state_editor)
