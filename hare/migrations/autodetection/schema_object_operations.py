from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.dictionary import Dictionary
from hare.migrations.autodetection.constants import CREATE_MODEL_DEFERRED_OPTIONS
from hare.migrations.autodetection.diffs.declarations import (
    DictionaryDiff,
    FunctionDiff,
    MaterializedViewDiff,
    PolicyDiff,
    SequenceDiff,
    ViewDiff,
)
from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.autodetection.diffs.grant_diff import GrantDiff
from hare.migrations.operations import (
    AlterRowLevelSecurity,
    HareOperation,
    RemoveFunction,
    RemoveSchemaObject,
    RemoveSequence,
    RenameSchemaObject,
)
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
    from hare.migrations.state.model_state import ModelState


class SchemaObjectOperations:
    """The operations on what models declare beside their tables - sequences, functions, views,
    materialized views, row level security, policies, grants - collected across the models of one
    migration and ordered around its other operations:

    - ``early_operations``, before any field or model operation: grants revoked, then policies,
      materialized views and views dropped, then renames - a dropped column must not be read by
      them any more;
    - ``late_operations``, after every table is created and changed: sequences, functions,
      dictionaries, views, materialized views, row level security, policies and grants created and changed, each type
      after the types it may use;
    - ``last_operations``: functions and sequences dropped, once nothing uses them.

    A view, materialized view or policy kept under its name whose SQL reads a column removed or
    changed by the migration is dropped early and created late - the database won't drop or change
    a column one of them depends on.

    Args:
        changed_column_names: The fields and columns the migration removes or changes.
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
    #: The types a changed column they read makes drop and create again.
    rebuilt_diff_classes: ClassVar[tuple[type[DeclaredSchemaObjectDiff], ...]] = (
        DictionaryDiff,
        ViewDiff,
        MaterializedViewDiff,
        PolicyDiff,
    )

    def __init__(self, changed_column_names: set[str]) -> None:
        self.changed_column_names = changed_column_names
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
            diff = diff_class(old_model_state, new_model_state)
            for operation in diff.get_operations():
                if isinstance(operation, RenameSchemaObject):
                    self.renames.append(operation)
                elif isinstance(operation, (RemoveFunction, RemoveSequence)):
                    self.last_removals_by_type[diff_class].append(operation)
                elif isinstance(operation, RemoveSchemaObject):
                    self.removals_by_type[diff_class].append(operation)
                else:
                    self.additions_by_type[diff_class].append(operation)
            if isinstance(diff, DeclaredSchemaObjectDiff) and diff_class in self.rebuilt_diff_classes:
                self._rebuild_readers_of_changed_columns(diff)
        old_setting = old_model_state.options.get(ModelOption.ROW_LEVEL_SECURITY)
        new_setting = new_model_state.options.get(ModelOption.ROW_LEVEL_SECURITY)
        if old_setting != new_setting:
            self.row_level_security_operations.append(
                AlterRowLevelSecurity(model_name=new_model_state.name, row_level_security=new_setting)
            )

    def _rebuild_readers_of_changed_columns(self, diff: DeclaredSchemaObjectDiff) -> None:
        """Drops early and creates late each object of a type kept under its name whose SQL reads a
        changed column - in place of its alter, if it has one.

        Args:
            diff: The type's diff of one model.
        """
        if not self.changed_column_names:
            return
        new_objects_by_name = {schema_object.name: schema_object for schema_object in diff.get_objects(diff.new_state)}
        additions = self.additions_by_type[type(diff)]
        for old_object in diff.get_objects(diff.old_state):
            new_object = new_objects_by_name.get(old_object.name)
            if new_object is None or not self._reads_changed_column(old_object):
                continue
            additions[:] = [
                operation
                for operation in additions
                if not (
                    isinstance(operation, diff.alter_operation_class or ())
                    and getattr(operation, "model_name", None) == diff.new_state.name
                    and operation.schema_object.name == old_object.name  # type: ignore[attr-defined]
                )
            ]
            self.removals_by_type[type(diff)].append(diff.get_remove_operation(old_object))
            additions.append(diff.get_add_operation(new_object))

    def _reads_changed_column(self, schema_object: Any) -> bool:
        """Whether a view's query or a policy's conditions name a changed field or column, or a
        dictionary reads one."""
        if isinstance(schema_object, Dictionary):
            return not self.changed_column_names.isdisjoint(schema_object.get_field_names())
        texts: list[Any] = []
        if isinstance(getattr(schema_object, "query", None), RawSQLTerm):
            texts.append(schema_object.query)
        for attribute in ("using", "with_check"):
            condition = getattr(schema_object, attribute, None)
            if condition is not None:
                texts.append(condition)
        return any(
            ModelBoundOperation._references_field_name(text, column_name)
            for text in texts
            for column_name in self.changed_column_names
        )

    @property
    def early_operations(self) -> list[HareOperation]:
        """Revokes and drops, the types created last first, then renames."""
        removals = [
            operation for diff_class in reversed(self.diff_classes) for operation in self.removals_by_type[diff_class]
        ]
        return removals + self.renames

    @property
    def late_operations(self) -> list[HareOperation]:
        """Creates and changes, the types others use first - row level security before policies."""
        operations: list[HareOperation] = []
        for diff_class in self.diff_classes:
            if diff_class is PolicyDiff:
                operations.extend(self.row_level_security_operations)
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
