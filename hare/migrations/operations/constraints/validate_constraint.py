from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.exceptions import ConfigurationError
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.schema_objects.object_types.constraint_object_type import ConstraintObjectType
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class ValidateConstraint(ModelBoundOperation):
    """Checks the existing rows against a constraint added ``NOT VALID`` (Postgres ``VALIDATE
    CONSTRAINT``) - with a lock that doesn't block writes. Nothing to do on SQLite, or going back.

    Args:
        model_name: The constrained model.
        name: The constraint's name.
    """

    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

    def describe(self) -> str:
        return f"Validate constraint {self.name} on {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        """Changes nothing - refuses a constraint of the model never added without checking the rows: of the
        declared ones only a check constraint is. A name the state doesn't know is left to the database - a
        relation's foreign key, or a constraint a RunSQL added.

        Raises:
            IncompatibleStateError: The model isn't in the state.
            ConfigurationError: The name is a declared constraint other than a check one.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        for constraint in ConstraintObjectType.get_objects(model_state):
            if constraint.name == self.name and not isinstance(constraint, CheckConstraint):
                raise ConfigurationError(
                    f"ValidateConstraint takes a check constraint - {self.name!r} on {self.model_name} is a "
                    f"{type(constraint).__name__}, checked as it is added."
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
        await state_editor.constraint_statements.validate_constraint(self._model(new_state, app_label), self.name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        pass
