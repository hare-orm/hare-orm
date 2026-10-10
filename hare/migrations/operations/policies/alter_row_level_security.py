from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.enums import RowLevelSecurity
from hare.exceptions import ConfigurationError
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class AlterRowLevelSecurity(ModelBoundOperation):
    """Turns row level security of a model's table on, forced or off - ``Meta.row_level_security``;
    going back restores the setting before.

    Args:
        model_name: The model.
        row_level_security: The new setting - ``RowLevelSecurity``, None for off.

    Raises:
        ConfigurationError: The setting is neither a ``RowLevelSecurity`` nor None.
    """

    def __init__(self, model_name: str, row_level_security: RowLevelSecurity | None) -> None:
        if row_level_security is not None and row_level_security not in set(RowLevelSecurity):
            raise ConfigurationError(
                f"AlterRowLevelSecurity.row_level_security must be one of {', '.join(RowLevelSecurity)} or None, "
                f"got {row_level_security!r}"
            )
        self.model_name = model_name
        self.row_level_security = RowLevelSecurity(row_level_security) if row_level_security is not None else None

    def describe(self) -> str:
        setting = self.row_level_security or "off"
        return f"Set row level security of {self.model_name} to {setting}"

    def get_setting(self, state: State, app_label: str) -> RowLevelSecurity | None:
        """The setting a state holds for the model.

        Args:
            state: The state.
            app_label: The migration's app.

        Returns:
            The setting; None for off.
        """
        return self.get_model_state(state, app_label, self.model_name).options.get(ModelOption.ROW_LEVEL_SECURITY)

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)
        if self.row_level_security is None:
            model_state.options.pop(ModelOption.ROW_LEVEL_SECURITY, None)
        else:
            model_state.options[ModelOption.ROW_LEVEL_SECURITY] = self.row_level_security
        state.reload_model(app_label, self.model_name)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        PolicyObjectType.raise_if_unsupported(state_editor)
        await state_editor.row_level_security_policies.alter_row_level_security(
            self._model(new_state, app_label), self.get_setting(old_state, app_label), self.row_level_security
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
        PolicyObjectType.raise_if_unsupported(state_editor)
        # Going back, new_state holds the setting before the change.
        await state_editor.row_level_security_policies.alter_row_level_security(
            self._model(new_state, app_label), self.row_level_security, self.get_setting(new_state, app_label)
        )
