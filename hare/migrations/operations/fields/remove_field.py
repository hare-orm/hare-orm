from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.field_condition import FieldCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.fields.field_add_remove_operation import FieldAddRemoveOperation
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RemoveField(FieldAddRemoveOperation):
    """Removes a field from a model and drops its column."""

    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

    def get_unmet_requirement_message(self, app_label: str, requirement: OperationDependency) -> str:
        return (
            f"Can't remove field {app_label}.{self.model_name}.{self.name}: a relation of app "
            f"{requirement.app_label!r} still points at it through to_field or goes through its model. Run "
            f"makemigrations for {requirement.app_label!r} in the same run, so its migration repointing the "
            "relation is applied first."
        )

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_field(app_label, self.model_name, self.name, DependencyType.REMOVED)]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """The relations pointing at the field through ``to_field``, and the many-to-many relations through
        the model - removed or repointed first."""
        dependencies = [
            OperationDependency.of_field(*model_key, field_name, DependencyType.CHANGED)
            for model_key, field_name in RelationDependencies.get_to_field_relations(
                state, app_label, self.model_name, self.name
            )
            if (model_key[0], model_key[1].lower(), field_name) != (app_label, self.model_name.lower(), self.name)
        ]
        dependencies.extend(
            OperationDependency.of_field(*model_key, field_name, DependencyType.CHANGED)
            for model_key, field_name in RelationDependencies.get_relations_through(state, app_label, self.model_name)
        )
        # A generated field computed from the field goes - or changes - first, in the same run.
        try:
            model_state = self.get_model_state(state, app_label, self.model_name)
        except IncompatibleStateError:
            return dependencies
        dependencies.extend(
            OperationDependency.of_field(app_label, self.model_name, field_name, DependencyType.CHANGED)
            for field_name, field in model_state.fields.items()
            if field_name != self.name and self.name in field.get_generated_from_field_names()
        )
        return dependencies

    def describe(self) -> str:
        return f"Remove field {self.name} from {self.model_name}"

    def get_effect(self, app_label: str, state: State, dialect: Dialect) -> OperationEffect:
        field = self.get_model_state(state, app_label, self.model_name).fields.get(self.name)
        dropped = "table of links" if isinstance(field, ManyToManyFieldInstance) else "column"
        return OperationEffect(
            operation=self,
            reversible=self.reversible,
            loses_data=True,
            reason=f"drops the {dropped} of {self.model_name}.{self.name} with its values",
        )

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        # Before the pop below - a raised error must leave the state unchanged.
        self._remove_meta_field_references(model_state, app_label)

        field = model_state.fields.pop(self.name, None)
        if not field:
            raise IncompatibleStateError(f"Field {self.name} is not present on model {app_label}.{self.model_name}")

        state.mark_models_changed(self.get_changed_model_keys(app_label, field))

    def _remove_meta_field_references(self, model_state: ModelState, app_label: str) -> None:
        """Drops the ``Meta`` indexes and constraints naming only the removed field; one that also
        names other fields raises - narrowing it would change what it guarantees. Raises too for a
        generated field computed from the removed field.

        Args:
            model_state: The state of the model the field is removed from.
            app_label: The model's app.

        Raises:
            ConfigurationError: A multi-field index or constraint, a ``Q`` condition, a trigger's or a
                policy's ``Q`` condition, the columns of a grant, a dictionary, a sequence's owner or a
                generated field names the field - raw SQL is the user's, not read.
        """
        primary_key_names = (
            model_state.pk_field_name if isinstance(model_state.pk_field_name, tuple) else (model_state.pk_field_name,)
        )
        if self.name in primary_key_names:
            raise self._get_reference_error(
                app_label,
                "the model's primary key",
                "A model keeps its primary key - change it with a hand-written RunSQL migration, or delete the model.",
            )
        self._reject_generated_field_references(model_state, app_label)
        self._remove_index_references(model_state, app_label)
        self._remove_constraint_references(model_state, app_label)
        for trigger in model_state.get_option_list(ModelOption.TRIGGERS):
            if self.condition_reads_field(trigger.when, self.name):
                raise self._get_reference_error(
                    app_label, f"referenced by trigger {trigger.name!r}", "Remove that trigger first."
                )
        for policy in model_state.get_option_list(ModelOption.POLICIES):
            if self.condition_reads_field(policy.using, self.name) or self.condition_reads_field(
                policy.with_check, self.name
            ):
                raise self._get_reference_error(
                    app_label, f"referenced by policy {policy.name!r}", "Remove or change that policy first."
                )
        for grant in model_state.get_option_list(ModelOption.GRANTS):
            if self.name in grant.columns:
                raise self._get_reference_error(
                    app_label, "named by a grant on its columns", "Remove or change that grant first."
                )
        for dictionary in model_state.get_option_list(ModelOption.DICTIONARIES):
            if self.name in dictionary.get_field_names():
                raise self._get_reference_error(
                    app_label, f"read by dictionary {dictionary.name!r}", "Remove or change that dictionary first."
                )
        for sequence in model_state.get_option_list(ModelOption.SEQUENCES):
            if sequence.owned_by == self.name:
                raise self._get_reference_error(
                    app_label, f"the owner of sequence {sequence.name!r}", "Change or remove that sequence first."
                )

    def _get_reference_error(self, app_label: str, reference: str, advice: str) -> ConfigurationError:
        """The error of removing a field something still refers to.

        Args:
            app_label: The model's app.
            reference: What the field still is for the thing referring to it.
            advice: What to do first.

        Returns:
            The error.
        """
        return ConfigurationError(
            f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's still {reference}. {advice}"
        )

    def _reject_generated_field_references(self, model_state: ModelState, app_label: str) -> None:
        """Raises when another field of the model is computed from the removed one.

        Args:
            model_state: The state of the model the field is removed from.
            app_label: The model's app.

        Raises:
            ConfigurationError: A field's source fields name it.
        """
        for other_field_name, other_field in model_state.fields.items():
            if other_field_name == self.name:
                continue
            if self.name in other_field.get_generated_from_field_names():
                raise self._get_reference_error(
                    app_label,
                    f"one of {type(other_field).__name__} {other_field_name!r}'s own source_fields",
                    f"Remove or edit {other_field_name!r} first.",
                )

    def _remove_index_references(self, model_state: ModelState, app_label: str) -> None:
        """Drops the ``Meta`` indexes on the removed field alone.

        Args:
            model_state: The state of the model the field is removed from.
            app_label: The model's app.

        Raises:
            ConfigurationError: An index of several fields, the included columns or the condition
                of an index name it.
        """
        indexes = model_state.get_option_list(ModelOption.INDEXES)
        remaining_indexes = []
        for index in indexes:
            fields = index.fields if isinstance(index, Index) else index
            if isinstance(index, Index) and self.name in index.include:
                raise self._get_reference_error(
                    app_label,
                    f"one of the include columns of Index {self._get_label(index)}",
                    "Remove that Index first.",
                )
            if self.name in fields:
                if len(fields) > 1:
                    raise self._get_reference_error(
                        app_label, f"part of an Index on {tuple(fields)!r}", "Remove that Index first."
                    )
                continue
            # A PartialIndex's Q condition can name a field that isn't among its own `.fields` - never
            # silently dropped: the index would then cover other rows.
            if isinstance(index, PartialIndex) and self.condition_reads_field(index.condition, self.name):
                raise self._get_reference_error(
                    app_label,
                    f"referenced by the condition of PartialIndex {self._get_label(index)}",
                    "Remove that Index first.",
                )
            remaining_indexes.append(index)
        if len(remaining_indexes) != len(indexes):
            model_state.set_option_list(ModelOption.INDEXES, remaining_indexes)

    def _remove_constraint_references(self, model_state: ModelState, app_label: str) -> None:
        """Drops the ``Meta`` constraints on the removed field alone.

        Args:
            model_state: The state of the model the field is removed from.
            app_label: The model's app.

        Raises:
            ConfigurationError: A constraint names the field together with something else.
        """
        constraints = model_state.get_option_list(ModelOption.CONSTRAINTS)
        remaining_constraints = []
        for constraint in constraints:
            referenced, can_silently_drop = self._get_constraint_reference(constraint)
            if not referenced:
                remaining_constraints.append(constraint)
            elif not can_silently_drop:
                raise self._get_reference_error(
                    app_label,
                    f"referenced by constraint {self._get_label(constraint)}",
                    "Remove that constraint first.",
                )
        if len(remaining_constraints) != len(constraints):
            model_state.set_option_list(ModelOption.CONSTRAINTS, remaining_constraints)

    def _get_constraint_reference(self, constraint: Any) -> tuple[bool, bool]:
        """How a constraint refers to the removed field.

        Args:
            constraint: The constraint.

        Returns:
            Whether it refers to the field, and whether it refers to nothing else - so that it goes
            with the field.
        """
        referenced, goes_with_field = self._get_constraint_field_reference(constraint)
        if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)):
            if self.name in constraint.include:
                return True, False
            if not referenced and self.condition_reads_field(constraint.condition, self.name):
                return True, False
        return referenced, goes_with_field

    def _get_constraint_field_reference(self, constraint: Any) -> tuple[bool, bool]:
        """How the fields, expressions or check of a constraint refer to the removed field.

        Args:
            constraint: The constraint.

        Returns:
            Whether they refer to the field, and whether they refer to nothing else.
        """
        if isinstance(constraint, (UniqueConstraint, ForeignKeyConstraint)):
            referenced = self.name in constraint.fields
            return referenced, referenced and len(constraint.fields) == 1
        if isinstance(constraint, ExclusionConstraint):
            read_field_names = constraint.get_read_field_names()
            return self.name in read_field_names, read_field_names == {self.name} and len(constraint.expressions) == 1
        if isinstance(constraint, CheckConstraint):
            # A Q check reading only this field goes with it, like the single-field cases above.
            return self.condition_reads_field(constraint.check, self.name), (
                isinstance(constraint.check, FieldCondition)
                and constraint.check.get_referenced_field_names() == {self.name}
            )
        return False, False

    @staticmethod
    def _get_label(schema_object: Any) -> str:
        """An index or a constraint as an error message names it.

        Args:
            schema_object: The index or the constraint.

        Returns:
            Its quoted name, its representation when it has none.
        """
        return f"{schema_object.name!r}" if schema_object.name else repr(schema_object)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self._remove_field_from_db(old_state, new_state, app_label, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        # Going back, old_state is without the field, new_state with it.
        await self._add_field_to_db(old_state, new_state, app_label, state_editor)
