from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.schema_references import SchemaReferences
from hare.declarations.declaration_values import DeclarationValues
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.constants import FIELD_NAMING_MODEL_OPTIONS, OBJECT_ADDING_OPERATIONS
from hare.migrations.autodetection.diffs.constraint_diff import ConstraintDiff
from hare.migrations.autodetection.diffs.index_diff import IndexDiff
from hare.migrations.autodetection.diffs.partition_diff import PartitionDiff
from hare.migrations.autodetection.diffs.state_field_diff import StateFieldDiff
from hare.migrations.autodetection.diffs.trigger_diff import TriggerDiff
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AlterField,
    AlterModelOptions,
    AlterModelSchema,
    AlterModelTable,
    HareOperation,
    RemoveConstraint,
    RemoveField,
    RemoveIndex,
    RenameField,
)
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption
from hare.query.key_columns import KeyColumns

if TYPE_CHECKING:
    from hare.migrations.state.state import State


class StateModelDiff:
    """The operations changing one model from its old state to its new one - its options, indexes, constraints,
    schema objects and fields."""

    def __init__(
        self,
        old_state: ModelState,
        new_state: ModelState,
        rename_questions: RenameQuestions | None = None,
        *,
        schema_states: tuple[State, State] | None = None,
    ) -> None:
        self.old_state = old_state
        self.new_state = new_state
        self.rename_questions = rename_questions or RenameQuestions()
        self.schema_states = schema_states
        #: Populated by generate_operations() - collects StateFieldDiff.warnings (see its own
        #: docstring) for this one model.
        self.warnings: list[str] = []
        #: Populated by generate_operations() - collects StateFieldDiff.data_loss_warnings (see
        #: its own docstring) for this one model.
        self.data_loss_warnings: list[str] = []
        #: The model's state before, as its field renames leave it - what its declared objects are
        #: compared with: an object naming a renamed field follows the rename, it isn't changed.
        self.old_state_after_renames = old_state
        #: Populated by generate_operations() - the model's field operations.
        self.field_operations: list[HareOperation] = []
        #: Populated by generate_operations() - every operation it returned.
        self.operations: list[HareOperation] = []

    @staticmethod
    def get_state_after_field_renames(model_state: ModelState, field_operations: list[HareOperation]) -> ModelState:
        """A model's state with the fields renamed as its field operations rename them.

        Args:
            model_state: The model's state.
            field_operations: The model's field operations.

        Returns:
            The state - a copy when a field is renamed.
        """
        renames = [operation for operation in field_operations if isinstance(operation, RenameField)]
        if not renames:
            return model_state
        renamed_state = model_state.clone()
        for rename in renames:
            rename.rename_in_model_state(renamed_state)
        return renamed_state

    def get_options_before_field_renames(
        self, options: dict[str, Any], field_operations: list[HareOperation]
    ) -> dict[str, Any]:
        """Options naming the fields by their names after the field renames, named by those before them - the
        options of the model ahead of the renames, which rename them as they run.

        Args:
            options: The options.
            field_operations: The model's field operations.

        Returns:
            The options; ``options`` itself when no field is renamed.
        """
        renames = [operation for operation in field_operations if isinstance(operation, RenameField)]
        if not renames:
            return options
        model_state = self.old_state_after_renames.clone()
        model_state.options = dict(options)
        for rename in reversed(renames):
            RenameField(rename.model_name, rename.new_name, rename.old_name).rename_in_model_state(model_state)
        return model_state.options

    def get_state_before_field_removals(
        self, field_operations: Sequence[HareOperation]
    ) -> tuple[ModelState, list[HareOperation]]:
        """Detach declared dependencies before dropping columns, using their original selectors.

        The normal object diff compares this remaining state with the target and restores
        surviving declarations after the fields are added. This also handles renamed objects
        and dependencies expressed in structured conditions or index expressions.
        """
        if self.schema_states is None:
            from hare.migrations.state.migration_environment import MigrationEnvironment
            from hare.migrations.state.state import State

            source_state = State(
                models={(self.old_state.app, self.old_state.name): self.old_state}, environment=MigrationEnvironment()
            )
        else:
            source_state = self.schema_states[0]
        removed_names = {
            attribute
            for operation in field_operations
            if isinstance(operation, RemoveField)
            for attribute in RelationDependencies.get_field_attribute_names(
                self.old_state.app, operation.name, self.old_state.fields.get(operation.name), source_state
            )
        }
        if not removed_names:
            return self.old_state, []
        state = self.old_state.clone()
        operations: list[HareOperation] = []
        for option, diff, get_names in (
            (ModelOption.INDEXES, IndexDiff(self.old_state, self.new_state), SchemaReferences.get_index_field_names),
            (
                ModelOption.CONSTRAINTS,
                ConstraintDiff(self.old_state, self.new_state),
                SchemaReferences.get_constraint_field_names,
            ),
        ):
            retained = []
            for schema_object in diff.get_objects(self.old_state):
                if removed_names.isdisjoint(get_names(schema_object)):
                    retained.append(schema_object)
                else:
                    operations.append(diff.get_remove_operation(schema_object))
            if retained:
                state.options[option] = tuple(retained)
            else:
                state.options.pop(option, None)
        return state, operations

    def get_removal_before_field_renames(
        self, operation: HareOperation, field_operations: list[HareOperation]
    ) -> HareOperation:
        """Use original logical names for unnamed selectors executed before field renames."""
        if not any(isinstance(field_operation, RenameField) for field_operation in field_operations):
            return operation
        if isinstance(operation, RemoveIndex) and not operation.name:
            index = operation.index if operation.index is not None else Index(fields=operation.fields)
            options = self.get_options_before_field_renames({ModelOption.INDEXES: (index,)}, field_operations)
            original_index = options[ModelOption.INDEXES][0]
            removal = copy(operation)
            if operation.index is not None:
                removal.index = original_index
            else:
                removal.fields = list(original_index.fields)
            return removal
        if isinstance(operation, RemoveConstraint) and not operation.name and operation.fields:
            options = self.get_options_before_field_renames(
                {ModelOption.CONSTRAINTS: (UniqueConstraint(fields=tuple(operation.fields)),)}, field_operations
            )
            constraint_removal = copy(operation)
            constraint_removal.fields = list(options[ModelOption.CONSTRAINTS][0].fields)
            return constraint_removal
        return operation

    @staticmethod
    def get_changed_columns(
        model_state: ModelState, field_operations: list[HareOperation]
    ) -> set[tuple[str | None, str, str]]:
        """The columns of a model's table its field operations change, ``(schema, table, column)`` each.

        Args:
            model_state: The model's state before the operations.
            field_operations: The model's field operations.

        Returns:
            The columns, by their names before the change.
        """
        table = model_state.db_table
        schema = model_state.options.get(ModelOption.SCHEMA)
        changed_columns: set[tuple[str | None, str, str]] = set()
        for operation in field_operations:
            if isinstance(operation, RenameField):
                field_name = operation.old_name
            elif isinstance(operation, (AlterField, RemoveField)):
                field_name = operation.name
            else:
                continue
            field = model_state.fields.get(field_name)
            column = ModelState.get_field_db_column(field_name, field) if field is not None else None
            if column is not None:
                changed_columns.add((schema, table, column))
        return changed_columns

    def check_primary_key_change(self, previous_key: str | tuple[str, ...]) -> None:
        """Refuse a changed key after all applicable field renames have updated its names."""
        if self.new_state.options.get(ModelOption.MANAGED) is False or previous_key == self.new_state.pk_field_name:
            return
        if self.schema_states is not None:
            before, after = self.schema_states
            old_binding = before.apps.get_binding(f"{self.old_state.app}.{self.old_state.name}")
            new_binding = after.apps.get_binding(f"{self.new_state.app}.{self.new_state.name}")
            if KeyColumns.get_source_columns(old_binding) == KeyColumns.get_source_columns(new_binding):
                return
        old_key, new_key = (
            repr(key) if key else "no primary key" for key in (previous_key, self.new_state.pk_field_name)
        )
        raise ConfigurationError(
            f"Changing the primary key of model {self.new_state.name} ({old_key} -> {new_key}) "
            "is not supported by migration autodetection. A rename of a key field is a "
            f"RenameField(model_name={self.new_state.name!r}, old_name=..., new_name=...) written by "
            "hand - makemigrations finds a rename only of a field otherwise left as it was; any other "
            "change needs a hand-written RunSQL migration (drop the old PRIMARY KEY constraint, add "
            "the new one)."
        )

    def generate_operations(self, *, check_primary_key: bool = True) -> list[HareOperation]:
        """Generate operations, deferring the key check only for the project-wide rename preview.

        The generator checks keys after all models' renames have updated incoming relations.
        Standalone model diffs check their local rename result immediately.
        """
        self.operations = self._get_operations(check_primary_key=check_primary_key)
        return self.operations

    def _get_operations(self, *, check_primary_key: bool) -> list[HareOperation]:
        if self.old_state == self.new_state:
            return []

        if self.new_state.options.get(ModelOption.MANAGED) is False:
            # The migrations don't manage this model's table (anymore) - only its options are
            # tracked, the table is left exactly as it is.
            old_options = StateSignatures.get_model_options_for_compare(self.old_state.options)
            new_options = StateSignatures.get_model_options_for_compare(self.new_state.options)
            if old_options == new_options:
                return []
            return [AlterModelOptions(name=self.new_state.name, options=new_options)]

        field_diff = StateFieldDiff(
            self.old_state, self.new_state, self.rename_questions, schema_states=self.schema_states
        )
        field_operations = field_diff.generate_operations()
        self.field_operations = list(field_operations)
        comparison_state, dependent_removals = self.get_state_before_field_removals(field_operations)
        self.old_state_after_renames = self.get_state_after_field_renames(comparison_state, field_operations)
        self.warnings.extend(field_diff.warnings)
        self.data_loss_warnings.extend(field_diff.data_loss_warnings)

        if check_primary_key:
            self.check_primary_key_change(self.old_state_after_renames.pk_field_name)

        if self.old_state.options.get(ModelOption.PK_WITHOUT_OVERLAPS, False) != self.new_state.options.get(
            ModelOption.PK_WITHOUT_OVERLAPS, False
        ):
            raise ConfigurationError(
                f"Changing CompositePrimaryKey(without_overlaps=...) of model {self.new_state.name} is not "
                "supported by migration autodetection - it needs a hand-written RunSQL migration (drop the "
                "old PRIMARY KEY constraint, add the new one)."
            )

        if self.old_state.options.get(ModelOption.TENANT_SCHEMA, False) != self.new_state.options.get(
            ModelOption.TENANT_SCHEMA, False
        ):
            raise ConfigurationError(
                f"Changing Meta.tenant_schema of model {self.new_state.name} is not supported by migration "
                "autodetection - its rows move between the shared schema and every tenant's schema; write "
                "the move as a hand-written migration (a new model, a RunPython copying the rows, then "
                "removing the old one)."
            )

        table_operations: list[HareOperation] = []
        # Migration state leaves the table unresolved ("") for a model without Meta.table - the
        # default name is used.
        old_table = self.old_state.db_table
        new_table = self.new_state.db_table
        old_explicit = ModelState.is_table_explicit(self.old_state.options)
        new_explicit = ModelState.is_table_explicit(self.new_state.options)
        if (old_table, old_explicit) != (new_table, new_explicit):
            table_operations.append(
                AlterModelTable(name=self.new_state.name, table=new_table if new_explicit else None)
            )
        old_schema = self.old_state.options.get(ModelOption.SCHEMA)
        new_schema = self.new_state.options.get(ModelOption.SCHEMA)
        if old_schema != new_schema:
            # The same for Meta.schema.
            table_operations.append(AlterModelSchema(name=self.new_state.name, schema=new_schema))
        # A trigger's body may read the changed columns.
        object_operations = [
            *IndexDiff(self.old_state_after_renames, self.new_state).get_operations(),
            *ConstraintDiff(self.old_state_after_renames, self.new_state).get_operations(),
            *TriggerDiff(
                self.old_state_after_renames,
                self.new_state,
                alters_in_place=not field_operations,
                changed_columns=frozenset(StateModelDiff.get_changed_columns(self.old_state, field_operations)),
            ).get_operations(),
        ]
        # Objects are renamed and removed before the fields change, added and changed after them.
        added_object_operations = [
            operation for operation in object_operations if isinstance(operation, OBJECT_ADDING_OPERATIONS)
        ]
        operations = [*dependent_removals]
        operations.extend(
            self.get_removal_before_field_renames(operation, field_operations)
            for operation in object_operations
            if not isinstance(operation, OBJECT_ADDING_OPERATIONS)
        )
        operations.extend(table_operations)
        partition_diff = PartitionDiff(self.old_state_after_renames, self.new_state)
        old_options = StateSignatures.get_model_options_for_compare(self.old_state_after_renames.options)
        # The partitions added and removed one at a time are written as their own operations below.
        new_options = partition_diff.get_options_before_partition_operations(
            StateSignatures.get_model_options_for_compare(self.new_state.options)
        )
        # `new_options` leaves out what has operations of its own - indexes and constraints would be
        # duplicated in the state. A change reading the changed fields is split around them.
        options_during_field_changes = (
            self._get_options_during_field_changes(new_options) if field_operations else new_options
        )
        # An option naming a field moves to the new one once the fields are added and before the old
        # one is removed - it names an existing field in every state.
        moved_field_options = [
            option
            for option in FIELD_NAMING_MODEL_OPTIONS
            if not DeclarationValues.are_equal(old_options.get(option), new_options.get(option))
        ]
        removed_fields: list[HareOperation] = []
        if moved_field_options and field_operations:
            options_during_field_changes = dict(options_during_field_changes)
            for option in moved_field_options:
                if option in old_options:
                    options_during_field_changes[str(option)] = old_options[option]
                else:
                    options_during_field_changes.pop(option, None)
        if field_operations and (
            moved_field_options or not DeclarationValues.are_equal(new_options, options_during_field_changes)
        ):
            # The options held while the fields change may still read a removed field - it is removed once
            # the new options are set.
            removed_fields = [
                operation
                for operation in field_operations
                if isinstance(operation, RemoveField) and operation.name not in field_diff.recreated_field_names
            ]
            field_operations = [operation for operation in field_operations if operation not in removed_fields]
        # Recreated columns briefly do not exist, even when their logical names are unchanged.
        # Options referring to them return only after AddField has restored the columns.
        for option in FIELD_NAMING_MODEL_OPTIONS:
            if options_during_field_changes.get(option) in field_diff.recreated_field_names:
                options_during_field_changes = dict(options_during_field_changes)
                options_during_field_changes.pop(option, None)
        # Compared by their declarations - hare's expressions compare by identity.
        if not DeclarationValues.are_equal(options_during_field_changes, old_options):
            operations.append(
                AlterModelOptions(
                    name=self.new_state.name,
                    options=self.get_options_before_field_renames(options_during_field_changes, field_operations),
                )
            )

        operations.extend(partition_diff.get_operations())
        self.data_loss_warnings.extend(partition_diff.data_loss_warnings)
        operations.extend(field_operations)
        if not DeclarationValues.are_equal(new_options, options_during_field_changes):
            operations.append(AlterModelOptions(name=self.new_state.name, options=new_options))
        operations.extend(removed_fields)

        # A unique guarantee switching between UniqueConstraint and Index(unique=True) over the same
        # fields: the add runs first, before the remove, so the fields are never unenforced.
        swap_field_sets = (
            self._unique_constraint_field_sets(self.new_state)
            & self._unique_index_field_sets(self.old_state_after_renames)
        ) | (
            self._unique_index_field_sets(self.new_state)
            & self._unique_constraint_field_sets(self.old_state_after_renames)
        )
        changed_field_names = set()
        for operation in self.field_operations:
            if isinstance(operation, (AddField, AlterField, RemoveField)):
                changed_field_names.add(operation.name)
            elif isinstance(operation, RenameField):
                changed_field_names.update((operation.old_name, operation.new_name))
        swap_additions = []
        if not table_operations:
            for operation in added_object_operations:
                if not self._adds_unique_over(operation, swap_field_sets):
                    continue
                if isinstance(operation, AddIndex):
                    names = SchemaReferences.get_index_field_names(operation.index)
                elif isinstance(operation, AddConstraint):
                    names = SchemaReferences.get_constraint_field_names(operation.constraint)
                else:
                    continue
                if changed_field_names.isdisjoint(names):
                    swap_additions.append(operation)
        return [
            *swap_additions,
            *operations,
            *(operation for operation in added_object_operations if operation not in swap_additions),
        ]

    @staticmethod
    def _adds_unique_over(operation: HareOperation, field_sets: set[frozenset[str]]) -> bool:
        """Whether an operation adds a unique index or constraint over one of the field sets.

        Args:
            operation: The operation.
            field_sets: The field sets.
        """
        if isinstance(operation, AddIndex):
            return bool(
                operation.index.unique and operation.index.fields and frozenset(operation.index.fields) in field_sets
            )
        if isinstance(operation, AddConstraint):
            return isinstance(operation.constraint, UniqueConstraint) and (
                frozenset(operation.constraint.fields) in field_sets
            )
        return False

    @staticmethod
    def _unique_constraint_field_sets(state: ModelState) -> set[frozenset[str]]:
        return {
            frozenset(constraint.fields)
            for constraint in state.options.get(ModelOption.CONSTRAINTS, ())
            if isinstance(constraint, UniqueConstraint) and constraint.condition is None
        }

    @staticmethod
    def _unique_index_field_sets(state: ModelState) -> set[frozenset[str]]:
        return {
            frozenset(index.fields)
            for index in map(IndexObjectType.as_schema_object, IndexObjectType.get_objects(state))
            if index.unique and index.fields
        }

    def _get_options_during_field_changes(self, new_options: dict[str, Any]) -> dict[str, Any]:
        """The model options the table holds while its fields change - each dialect's table
        options as ``TableOptions.get_options_during_field_changes()`` gives them.

        Args:
            new_options: The compared options after the change.

        Returns:
            The options; ``new_options`` itself when they are the same.
        """
        new_table_options = new_options.get(ModelOption.TABLE_OPTIONS)
        if not new_table_options:
            return new_options
        old_table_options = self.old_state_after_renames.options.get(ModelOption.TABLE_OPTIONS) or ()
        table_options_during_field_changes = []
        for table_options in new_table_options:
            old_table_options_of_dialect = next(
                (entry for entry in old_table_options if type(entry) is type(table_options)), None
            )
            options_during_field_changes = table_options.get_options_during_field_changes(old_table_options_of_dialect)
            if old_table_options_of_dialect is not None or not self._holds_default_options(
                options_during_field_changes
            ):
                table_options_during_field_changes.append(options_during_field_changes)
        if DeclarationValues.are_equal(table_options_during_field_changes, list(new_table_options)):
            return new_options
        options = dict(new_options)
        if table_options_during_field_changes:
            options[ModelOption.TABLE_OPTIONS] = type(new_table_options)(table_options_during_field_changes)
        else:
            del options[ModelOption.TABLE_OPTIONS]
        return options

    @staticmethod
    def _holds_default_options(table_options: Any) -> bool:
        """Whether table options hold the default of every option - the same as declaring none.

        Args:
            table_options: The options.

        Returns:
            True when every option is its declared default.
        """
        return all(
            option.default is not dataclasses.MISSING and getattr(table_options, option.name) == option.default
            for option in dataclasses.fields(table_options)
            if option.init
        )
