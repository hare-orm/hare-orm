from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.ddl.schema_objects.view_creation_order import ViewCreationOrder
from hare.migrations.autodetection.constants import CREATE_MODEL_DEFERRED_OPTIONS
from hare.migrations.autodetection.diffs.declarations import DictionaryDiff, FunctionDiff, SequenceDiff
from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.autodetection.diffs.grant_diff import GrantDiff
from hare.migrations.autodetection.diffs.materialized_view_diff import MaterializedViewDiff
from hare.migrations.autodetection.diffs.policy_diff import PolicyDiff
from hare.migrations.autodetection.diffs.view_diff import ViewDiff
from hare.migrations.operations import (
    AlterRowLevelSecurity,
    HareOperation,
    RemoveSchemaObject,
    RenameSchemaObject,
)
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
    from hare.migrations.state.model_state import ModelState


class SchemaObjectOperations:
    """The operations on what models declare beside their tables - sequences, functions, views,
    materialized views, row level security, policies, grants - collected across the models of one
    migration and ordered around its other operations:

    - ``early_removals``, before any model is renamed and before any field or model operation:
      grants revoked, then policies, materialized views and views dropped, by the model's old name - a
      dropped column must not be read by them any more;
    - ``renames``, once the models have their new names;
    - ``late_operations``, after every table is created and changed: sequences, functions,
      dictionaries, views, materialized views, row level security, policies and grants created and changed, each type
      after the types it may use;
    - ``last_operations``: functions and sequences dropped, once nothing uses them.

    An object whose declaration is unchanged is left alone - raw SQL is the user's: a column it reads
    changing is the user's to account for, as with ``RunSQL``. A changed one is removed and added when
    the migration changes any column or table - it may read them.

    Args:
        alters_in_place: Whether a changed object is altered rather than removed and added.
        changed_columns: The columns the migration changes, ``(table, column)`` each - an object reading
            one is removed and added.
    """

    #: The types, in the order they are created.
    diff_classes: ClassVar[tuple[type[SchemaObjectDiff], ...]] = (
        SequenceDiff,
        FunctionDiff,
        DictionaryDiff,
        ViewDiff,
        MaterializedViewDiff,
        PolicyDiff,
        GrantDiff,
    )

    def __init__(
        self, alters_in_place: bool = True, changed_columns: frozenset[tuple[str, str]] = frozenset()
    ) -> None:
        self.alters_in_place = alters_in_place
        self.changed_columns = changed_columns
        self.removals_by_type: dict[type[SchemaObjectDiff], list[HareOperation]] = {
            diff_class: [] for diff_class in self.diff_classes
        }
        self.renames: list[HareOperation] = []
        self.additions_by_type: dict[type[SchemaObjectDiff], list[HareOperation]] = {
            diff_class: [] for diff_class in self.diff_classes
        }
        self.row_level_security_operations: list[HareOperation] = []
        self.last_removals_by_type: dict[type[SchemaObjectDiff], list[HareOperation]] = {
            diff_class: [] for diff_class in self.diff_classes
        }
        #: The views and materialized views removed, by the removing operation - dropped in the reverse of
        #: the order they are created in.
        self.removed_views: dict[int, Any] = {}

    @staticmethod
    def without_deferred_options(options: dict[str, Any]) -> dict[str, Any]:
        """A new model's options without those its own operations add after every table exists.

        Args:
            options: The model's options.

        Returns:
            The options ``CreateModel`` keeps.
        """
        return {key: value for key, value in options.items() if key not in CREATE_MODEL_DEFERRED_OPTIONS}

    def add_created_model(self, model_state: ModelState) -> None:
        """Collects the operations adding what a model ``CreateModel`` creates without - its
        deferred options.

        Args:
            model_state: The new model's state.
        """
        created_state = copy(model_state)
        created_state.options = self.without_deferred_options(model_state.options)
        self.add_changed_model(created_state, model_state)

    def add_changed_model(self, old_model_state: ModelState, new_model_state: ModelState) -> None:
        """Collects the operations taking what a model declares from one state to another.

        Args:
            old_model_state: The model's state before.
            new_model_state: The model's state after.
        """
        for diff_class in self.diff_classes:
            diff = diff_class(old_model_state, new_model_state, self.alters_in_place, self.changed_columns)
            removed_last = isinstance(diff, DeclaredSchemaObjectDiff) and diff.removed_last
            for operation in diff.get_operations():
                if isinstance(operation, RenameSchemaObject):
                    self.renames.append(operation)
                elif removed_last and isinstance(operation, RemoveSchemaObject):
                    self.last_removals_by_type[diff_class].append(operation)
                elif isinstance(operation, RemoveSchemaObject):
                    self.removals_by_type[diff_class].append(operation)
                    if diff_class in (ViewDiff, MaterializedViewDiff):
                        self.removed_views[id(operation)] = next(
                            view for view in diff.get_objects(old_model_state) if view.name == operation.name
                        )
                else:
                    self.additions_by_type[diff_class].append(operation)
        old_setting = old_model_state.options.get(ModelOption.ROW_LEVEL_SECURITY)
        new_setting = new_model_state.options.get(ModelOption.ROW_LEVEL_SECURITY)
        if old_setting != new_setting:
            self.row_level_security_operations.append(
                AlterRowLevelSecurity(model_name=new_model_state.name, row_level_security=new_setting)
            )

    @property
    def early_removals(self) -> list[HareOperation]:
        """Revokes and drops, the types created last first - a view or a materialized view before the ones
        it reads."""
        operations: list[HareOperation] = []
        for diff_class in reversed(self.diff_classes):
            if diff_class is MaterializedViewDiff:
                view_removals = [*self.removals_by_type[ViewDiff], *self.removals_by_type[MaterializedViewDiff]]
                operations.extend(
                    reversed(
                        ViewCreationOrder.get_ordered(
                            view_removals, lambda operation: self.removed_views[id(operation)]
                        )
                    )
                )
            elif diff_class is not ViewDiff:
                operations.extend(self.removals_by_type[diff_class])
        return operations

    @property
    def late_operations(self) -> list[HareOperation]:
        """Creates and changes, the types others use first - row level security before policies."""
        operations: list[HareOperation] = []
        for diff_class in self.diff_classes:
            if diff_class is PolicyDiff:
                operations.extend(self.row_level_security_operations)
            if diff_class is ViewDiff:
                # A view and a materialized view are created each after the ones it reads.
                view_operations = [*self.additions_by_type[ViewDiff], *self.additions_by_type[MaterializedViewDiff]]
                operations.extend(
                    ViewCreationOrder.get_ordered(
                        view_operations, lambda operation: cast("Any", operation).schema_object
                    )
                )
            elif diff_class is not MaterializedViewDiff:
                operations.extend(self.additions_by_type[diff_class])
        return operations

    @property
    def last_operations(self) -> list[HareOperation]:
        """Functions dropped, then sequences - a function may take from a sequence."""
        return [
            operation
            for diff_class in reversed(self.diff_classes)
            for operation in self.last_removals_by_type[diff_class]
        ]
