from __future__ import annotations

import dataclasses
from typing import Any

from hare.classes.comparable_values import ComparableValues
from hare.ddl.constraints.unique_constraint import UniqueConstraint
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
    AddIndex,
    AlterField,
    AlterModelOptions,
    AlterModelSchema,
    AlterModelTable,
    HareOperation,
    RemoveField,
    RenameField,
)
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption


class StateModelDiff:
    """The operations changing one model from its old state to its new one - its options, indexes, constraints,
    schema objects and fields."""

    def __init__(
        self, old_state: ModelState, new_state: ModelState, rename_questions: RenameQuestions | None = None
    ) -> None:
        self.old_state = old_state
        self.new_state = new_state
        self.rename_questions = rename_questions or RenameQuestions()
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

    @staticmethod
    def get_changed_columns(model_state: ModelState, field_operations: list[HareOperation]) -> set[tuple[str, str]]:
        """The columns of a model's table its field operations change, ``(table, column)`` each.

        Args:
            model_state: The model's state before the operations.
            field_operations: The model's field operations.

        Returns:
            The columns, by their names before the change.
        """
        table = model_state.table or model_state.name.lower()
        changed_columns: set[tuple[str, str]] = set()
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
                changed_columns.add((table, column))
        return changed_columns

    def _pk_rename_operation(self, field_operations: list[HareOperation]) -> RenameField | None:
        """The ``RenameField`` among ``field_operations`` that explains the primary key's name change -
        for a composite key, the one component renamed.

        Returns:
            The operation, None when the change isn't a plain rename.
        """
        old_pk = self.old_state.pk_field_name
        new_pk = self.new_state.pk_field_name
        if isinstance(old_pk, tuple) != isinstance(new_pk, tuple):
            return None
        if isinstance(old_pk, tuple):
            if not isinstance(new_pk, tuple) or len(old_pk) != len(new_pk):
                return None
            renamed_positions = [
                (old_name, new_name) for old_name, new_name in zip(old_pk, new_pk, strict=True) if old_name != new_name
            ]
            if len(renamed_positions) != 1:
                return None
            old_pk, new_pk = renamed_positions[0]
        for operation in field_operations:
            if isinstance(operation, RenameField) and operation.old_name == old_pk and operation.new_name == new_pk:
                return operation
        return None

    def generate_operations(self) -> list[HareOperation]:
        """The operations taking the model from its old state to its new one, kept in ``operations``."""
        self.operations = self._get_operations()
        return self.operations

    def _get_operations(self) -> list[HareOperation]:
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

        field_diff = StateFieldDiff(self.old_state, self.new_state, self.rename_questions)
        field_operations = field_diff.generate_operations()
        self.field_operations = list(field_operations)
        self.old_state_after_renames = self.get_state_after_field_renames(self.old_state, field_operations)
        self.warnings.extend(field_diff.warnings)
        self.data_loss_warnings.extend(field_diff.data_loss_warnings)

        if (
            self.old_state.pk_field_name != self.new_state.pk_field_name
            and self._pk_rename_operation(field_operations) is None
        ):
            # Changing which fields make up the primary key has no operation - rejected. A plain
            # rename of the key field was handled above.
            old_key, new_key = (
                repr(key) if key else "no primary key"
                for key in (self.old_state.pk_field_name, self.new_state.pk_field_name)
            )
            raise ConfigurationError(
                f"Changing the primary key of model {self.new_state.name} ({old_key} -> {new_key}) "
                "is not supported by migration autodetection. A rename of a key field is a "
                f"RenameField(model_name={self.new_state.name!r}, old_name=..., new_name=...) written by "
                "hand - makemigrations finds a rename only of a field otherwise left as it was; any other "
                "change needs a hand-written RunSQL migration (drop the old PRIMARY KEY constraint, add "
                "the new one)."
            )

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

        operations: list[HareOperation] = []
        # Migration state leaves the table unresolved ("") for a model without Meta.table - the
        # default name is used.
        old_table = self.old_state.table or self.old_state.name.lower()
        new_table = self.new_state.table or self.new_state.name.lower()
        if old_table != new_table:
            # A Meta.table change has its own operation - AlterModelOptions touches no database.
            operations.append(AlterModelTable(name=self.new_state.name, table=new_table))
        old_schema = self.old_state.options.get(ModelOption.SCHEMA)
        new_schema = self.new_state.options.get(ModelOption.SCHEMA)
        if old_schema != new_schema:
            # The same for Meta.schema.
            operations.append(AlterModelSchema(name=self.new_state.name, schema=new_schema))
        # A trigger's body may read the changed columns.
        object_operations = [
            *IndexDiff(self.old_state_after_renames, self.new_state).get_operations(),
            *ConstraintDiff(self.old_state_after_renames, self.new_state).get_operations(),
            *TriggerDiff(
                self.old_state_after_renames, self.new_state, alters_in_place=not field_operations
            ).get_operations(),
        ]
        # Objects are renamed and removed before the fields change, added and changed after them.
        added_object_operations = [
            operation for operation in object_operations if isinstance(operation, OBJECT_ADDING_OPERATIONS)
        ]
        operations.extend(
            operation for operation in object_operations if not isinstance(operation, OBJECT_ADDING_OPERATIONS)
        )
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
            if not ComparableValues.are_equal(old_options.get(option), new_options.get(option))
        ]
        removed_fields: list[HareOperation] = []
        if moved_field_options and field_operations:
            options_during_field_changes = dict(options_during_field_changes)
            for option in moved_field_options:
                if option in old_options:
                    options_during_field_changes[option] = old_options[option]
                else:
                    options_during_field_changes.pop(option, None)
            removed_fields = [operation for operation in field_operations if isinstance(operation, RemoveField)]
            field_operations = [operation for operation in field_operations if not isinstance(operation, RemoveField)]
        # Compared by their declarations - hare's expressions compare by identity.
        if not ComparableValues.are_equal(options_during_field_changes, old_options):
            operations.append(AlterModelOptions(name=self.new_state.name, options=options_during_field_changes))

        operations.extend(partition_diff.get_operations())
        self.data_loss_warnings.extend(partition_diff.data_loss_warnings)
        operations.extend(field_operations)
        if not ComparableValues.are_equal(new_options, options_during_field_changes):
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
        swap_additions = [
            operation for operation in added_object_operations if self._adds_unique_over(operation, swap_field_sets)
        ]
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
            for index in StateSignatures.normalize_indexes(state.options.get(ModelOption.INDEXES, ()))
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
        if ComparableValues.are_equal(table_options_during_field_changes, list(new_table_options)):
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
