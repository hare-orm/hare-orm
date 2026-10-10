from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.constraint_object_type import ConstraintObjectType
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class AddConstraint(AddSchemaObject):
    """Adds a constraint to a model.

    Args:
        model_name: The model.
        constraint: The constraint.
        not_valid: For a check constraint: the existing rows needn't pass it yet (Postgres ``NOT
            VALID``) - only new and updated rows are checked, so adding it doesn't scan and lock
            the table; a later ``ValidateConstraint`` checks the rest. SQLite adds it the plain way.
        using_index: For a named unique constraint without a condition: the unique index of the
            model it takes over instead of building its own (Postgres ``UNIQUE USING INDEX``) - an
            index built before with ``AddIndex(..., concurrently=True)``, which leaves the model's
            ``Meta.indexes`` and becomes the constraint's, under the constraint's name.

    Raises:
        ConfigurationError: ``not_valid`` for a constraint other than a check one; ``using_index``
            for a constraint other than a named unique one without a condition.
    """

    object_type: ClassVar[type[ConstraintObjectType]] = ConstraintObjectType

    def __init__(
        self,
        model_name: str,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        *,
        not_valid: bool = False,
        using_index: str | None = None,
    ) -> None:
        if not_valid and not isinstance(constraint, CheckConstraint):
            raise ConfigurationError(f"not_valid=True takes a CheckConstraint, got {type(constraint).__name__}")
        if using_index is not None:
            if not isinstance(using_index, str) or not using_index:
                raise ConfigurationError(f"using_index must be an index name, got {using_index!r}")
            if not isinstance(constraint, UniqueConstraint) or not constraint.name or constraint.condition is not None:
                raise ConfigurationError("using_index takes a named UniqueConstraint without a condition")
        super().__init__(model_name)
        self.constraint = constraint
        self.not_valid = not_valid
        self.using_index = using_index

    @property
    def schema_object(self) -> Any:
        return self.constraint

    def get_type_options(self) -> dict[str, Any]:
        return {"not_valid": self.not_valid}

    def get_description_action(self) -> str:
        return "Add not-valid" if self.not_valid else "Add"

    def describe(self) -> str:
        description = super().describe()
        return f"{description} using index {self.using_index}" if self.using_index else description

    def get_used_index(self, state: State, app_label: str) -> Index:
        """The index ``using_index`` names, as the model's state holds it.

        Args:
            state: A state with the index.
            app_label: The migration's app.

        Returns:
            The index.

        Raises:
            IncompatibleStateError: The model has no index of that name.
        """
        model_state = self.get_model_state(state, app_label, self.model_name)
        for index in IndexObjectType.get_objects(model_state):
            if isinstance(index, Index) and index.name == self.using_index:
                return index
        raise IncompatibleStateError(f"Index {self.using_index} is not present on {model_state.name}")

    def state_forward(self, app_label: str, state: State) -> None:
        if self.using_index is not None:
            model_state = self.get_model_state(state, app_label, self.model_name)
            IndexObjectType.update_objects(model_state, removed=self.get_used_index(state, app_label))
        super().state_forward(app_label, state)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if self.using_index is None:
            await super().database_forward(app_label, old_state, new_state, state_editor)
            return
        if not state_editor:
            return
        await state_editor.constraint_statements.add_unique_constraint_using_index(
            self._model(new_state, app_label), cast("UniqueConstraint", self.constraint), self.using_index
        )

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await super().database_backward(app_label, old_state, new_state, state_editor)
        if self.using_index is None or not state_editor:
            return
        # Dropping the constraint dropped the index it took over - built again, for whatever
        # removes it going further back.
        await state_editor.add_index(self._model(new_state, app_label), self.get_used_index(new_state, app_label))
