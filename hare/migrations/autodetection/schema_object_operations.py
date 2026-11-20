from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.schema_objects.view_creation_order import ViewCreationOrder
from hare.migrations.autodetection.constants import CREATE_MODEL_DEFERRED_OPTIONS
from hare.migrations.autodetection.diffs.declarations import DictionaryDiff, FunctionDiff, SequenceDiff
from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.autodetection.diffs.grant_diff import GrantDiff
from hare.migrations.autodetection.diffs.materialized_view_diff import MaterializedViewDiff
from hare.migrations.autodetection.diffs.policy_diff import PolicyDiff
from hare.migrations.autodetection.diffs.view_diff import ViewDiff
from hare.migrations.operations import (
    AddSchemaObject,
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
    - ``last_operations``: functions and sequences with no replacement dropped, once nothing uses them.

    An object whose declaration is unchanged is left alone - raw SQL is the user's: a column it reads
    changing is the user's to account for, as with ``RunSQL``. A changed one is removed and added when
    the migration changes any column or table - it may read them.

    Args:
        alters_in_place: Whether a changed object is altered rather than removed and added.
        replaces_functions: Whether a function needs a new physical definition. Changed views and
            policies are detached before its replacement because their SQL may call it.
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
        self,
        alters_in_place: bool = True,
        *,
        replaces_functions: bool = False,
    ) -> None:
        self.alters_in_place = alters_in_place
        self.replaces_functions = replaces_functions
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
        self.view_schemas: dict[int, str | None] = {}

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

    def add_changed_model(
        self,
        old_model_state: ModelState,
        new_model_state: ModelState,
        *,
        changed_columns: frozenset[tuple[str | None, str, str]] = frozenset(),
    ) -> None:
        """Collects the operations taking what a model declares from one state to another.

        Args:
            old_model_state: The model's state before.
            new_model_state: The model's state after.
            changed_columns: The columns changing on this model's connection, by schema, table and column.
        """
        for diff_class in self.diff_classes:
            alters_in_place = (
                diff_class is SequenceDiff
                or self.alters_in_place
                and not (self.replaces_functions and diff_class in (ViewDiff, MaterializedViewDiff, PolicyDiff))
            )
            diff = diff_class(old_model_state, new_model_state, alters_in_place, changed_columns)
            removed_last = isinstance(diff, DeclaredSchemaObjectDiff) and diff.removed_last
            operations = diff.get_operations()
            replacement_names = (
                {operation.schema_object.name for operation in operations if isinstance(operation, AddSchemaObject)}
                if removed_last
                else set()
            )
            for operation in operations:
                if diff_class in (ViewDiff, MaterializedViewDiff):
                    state = old_model_state if isinstance(operation, RemoveSchemaObject) else new_model_state
                    self.view_schemas[id(operation)] = state.options.get(ModelOption.SCHEMA)
                if isinstance(operation, RenameSchemaObject):
                    self.renames.append(operation)
                elif removed_last and isinstance(operation, RemoveSchemaObject):
                    if operation.name in replacement_names:
                        # The old declaration must leave before the new one can take its name.
                        self.additions_by_type[diff_class].append(operation)
                    else:
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
                            view_removals,
                            lambda operation: self.removed_views[id(operation)],
                            get_schema=lambda operation: self.view_schemas[id(operation)],
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
                        view_operations,
                        lambda operation: operation.schema_object,  # type: ignore[attr-defined]
                        get_schema=lambda operation: self.view_schemas[id(operation)],
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
