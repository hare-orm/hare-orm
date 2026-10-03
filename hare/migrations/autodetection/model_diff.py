from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.constraint_diff import ConstraintDiff
from hare.migrations.autodetection.field_diff import StateFieldDiff
from hare.migrations.autodetection.index_diff import IndexDiff
from hare.migrations.autodetection.partition_diff import PartitionDiff
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.autodetection.trigger_diff import TriggerDiff
from hare.migrations.operations import (
    AddConstraint,
    AddIndex,
    AddTrigger,
    AlterField,
    AlterModelOptions,
    AlterModelSchema,
    AlterModelTable,
    AlterTrigger,
    HareOperation,
    RenameField,
)
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.migrations.state.project.model_state import ModelState


class StateModelDiff:
    def __init__(self, old_state: ModelState, new_state: ModelState) -> None:
        self.old_state = old_state
        self.new_state = new_state
        #: Populated by generate_operations() - collects StateFieldDiff.warnings (see its own
        #: docstring) for this one model.
        self.warnings: list[str] = []
        #: Populated by generate_operations() - collects StateFieldDiff.data_loss_warnings (see
        #: its own docstring) for this one model.
        self.data_loss_warnings: list[str] = []

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
            renamed_positions = [(o, n) for o, n in zip(old_pk, new_pk, strict=True) if o != n]
            if len(renamed_positions) != 1:
                return None
            old_pk, new_pk = renamed_positions[0]
        for operation in field_operations:
            if isinstance(operation, RenameField) and operation.old_name == old_pk and operation.new_name == new_pk:
                return operation
        return None

    def generate_operations(self) -> list[HareOperation]:
        if self.old_state == self.new_state:
            return []

        if self.new_state.options.get(ModelOption.MANAGED) is False:
            # The migrations don't manage this model's table (anymore) - only its options are
            # tracked, the table is left exactly as it is.
            old_options = StateSignatures._get_model_options_for_compare(self.old_state.options)
            new_options = StateSignatures._get_model_options_for_compare(self.new_state.options)
            if old_options == new_options:
                return []
            return [AlterModelOptions(name=self.new_state.name, options=new_options)]

        field_diff = StateFieldDiff(self.old_state, self.new_state)
        field_operations = field_diff.generate_operations()
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
                "is not supported by migration autodetection - it needs a hand-written RunSQL "
                "migration (drop the old PRIMARY KEY constraint, add the new one)."
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
        index_flag_altered_field_names = self._get_index_flag_altered_field_names(field_operations)
        operations.extend(IndexDiff(self.old_state, self.new_state, index_flag_altered_field_names).get_operations())
        operations.extend(ConstraintDiff(self.old_state, self.new_state).get_operations())
        operations.extend(TriggerDiff(self.old_state, self.new_state).get_operations())
        partition_diff = PartitionDiff(self.old_state, self.new_state)
        old_options = StateSignatures._get_model_options_for_compare(self.old_state.options)
        # The partitions added and removed one at a time are written as their own operations below.
        new_options = partition_diff.get_options_before_partition_operations(
            StateSignatures._get_model_options_for_compare(self.new_state.options)
        )
        if old_options != new_options:
            # `new_options` leaves out what has operations of its own - indexes and constraints
            # would be duplicated in the state.
            operations.append(
                AlterModelOptions(
                    name=self.new_state.name,
                    options=new_options,
                )
            )

        operations.extend(partition_diff.get_operations())
        self.data_loss_warnings.extend(partition_diff.data_loss_warnings)
        operations.extend(field_operations)

        # A unique guarantee switching between UniqueConstraint and Index(unique=True) over the same
        # fields: the add runs before the remove, so the fields are never unenforced.
        swap_field_sets = (
            self._unique_constraint_field_sets(self.new_state) & self._unique_index_field_sets(self.old_state)
        ) | (self._unique_index_field_sets(self.new_state) & self._unique_constraint_field_sets(self.old_state))

        def is_swap_add(op: HareOperation) -> bool:
            if isinstance(op, AddIndex):
                return bool(op.index.unique and op.index.fields and frozenset(op.index.fields) in swap_field_sets)
            if isinstance(op, AddConstraint):
                return isinstance(op.constraint, UniqueConstraint) and (
                    frozenset(op.constraint.fields) in swap_field_sets
                )
            return False

        # Classes that must always be last
        always_last = (AddIndex, AddConstraint, AddTrigger, AlterTrigger)

        def sort_key(op: HareOperation) -> int:
            # A swap-add depends on nothing else in the migration - it runs first.
            if is_swap_add(op):
                return -1
            return int(isinstance(op, always_last))

        operations = sorted(operations, key=sort_key)
        return operations

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
            for index in StateSignatures._normalize_indexes(state.options.get(ModelOption.INDEXES, ()))
            if index.unique and index.fields
        }

    def _get_index_flag_altered_field_names(self, field_operations: list[HareOperation]) -> set[str]:
        """Names of existing fields whose own index flag flips under an AlterField - the AlterField's
        own DDL and state update already create/drop that field's implicit index, so a companion
        AddIndex/RemoveIndex for it would run the same CREATE/DROP INDEX a second time.

        Args:
            field_operations: The field-level operations generated for this model.

        Returns:
            The field names whose implicit index is owned by their AlterField.
        """
        field_names: set[str] = set()
        for operation in field_operations:
            if not isinstance(operation, AlterField):
                continue
            old_field = self.old_state.fields.get(operation.name)
            new_field = self.new_state.fields.get(operation.name)
            if old_field is None or new_field is None:
                continue
            if (old_field.index and not old_field.pk) != (new_field.index and not new_field.pk):
                field_names.add(operation.name)
        return field_names
