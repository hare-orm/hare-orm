from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError
from hare.fields.generated import GeneratedField
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.fields.field_add_remove_operation import FieldAddRemoveOperation
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.state.project.state import State
from hare.models.enums import ModelOption
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RemoveField(FieldAddRemoveOperation):
    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

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

        models_to_reload = {(app_label, self.model_name)}
        if isinstance(field, DIRECT_RELATION_FIELDS):
            models_to_reload.add(state.apps.split_reference(field.model_name))

        state.reload_models(models_to_reload)

    def _remove_meta_field_references(self, model_state: ModelState, app_label: str) -> None:
        """Drops the ``Meta`` indexes and constraints naming only the removed field; one that also
        names other fields raises - narrowing it would change what it guarantees. Raises too for a
        generated field computed from the removed field.

        Raises:
            ConfigurationError: A multi-field index or constraint, a raw SQL condition, a trigger or
                a generated field refers to the field.
        """
        for other_field_name, other_field in model_state.fields.items():
            if other_field_name == self.name:
                continue
            if isinstance(other_field, GeneratedField):
                expressions_to_check = (
                    other_field.expression.values()
                    if isinstance(other_field.expression, dict)
                    else [other_field.expression]
                )
                if any(self._references_field_name(expr, self.name) for expr in expressions_to_check):
                    raise ConfigurationError(
                        f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                        f"still referenced by GeneratedField {other_field_name!r}'s own expression. "
                        f"Remove or edit {other_field_name!r} first."
                    )
            elif self.name in other_field.get_generated_from_field_names():
                raise ConfigurationError(
                    f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                    f"still one of {type(other_field).__name__} {other_field_name!r}'s own source_fields. "
                    f"Remove or edit {other_field_name!r} first."
                )

        indexes = model_state.get_option_list(ModelOption.INDEXES)
        if indexes:
            remaining_indexes = []
            for index in indexes:
                fields = index.fields if isinstance(index, Index) else index
                if isinstance(index, Index) and self.name in index.include:
                    index_label = f"{index.name!r}" if index.name else repr(index)
                    raise ConfigurationError(
                        f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                        f"still one of the include columns of Index {index_label}. Remove that Index first."
                    )
                if self.name in fields:
                    if len(fields) > 1:
                        raise ConfigurationError(
                            f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                            f"still part of an Index on {tuple(fields)!r}. Remove that Index first."
                        )
                    continue
                # A PartialIndex's condition can name a column that isn't in its own `.fields`
                # at all (a predicate over a different column) - never silently dropped: the
                # index would then cover other rows.
                if (
                    isinstance(index, PartialIndex)
                    and index.condition is not None
                    and self._references_field_name(index.condition, self.name)
                ):
                    index_label = f"{index.name!r}" if index.name else repr(index)
                    raise ConfigurationError(
                        f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                        f"still referenced by the condition of PartialIndex {index_label}. Remove that "
                        "Index first."
                    )
                remaining_indexes.append(index)
            if len(remaining_indexes) != len(indexes):
                model_state.set_option_list(ModelOption.INDEXES, remaining_indexes)

        constraints = model_state.get_option_list(ModelOption.CONSTRAINTS)
        if constraints:
            remaining_constraints = []
            for constraint in constraints:
                if isinstance(constraint, (UniqueConstraint, ForeignKeyConstraint)):
                    referenced = self.name in constraint.fields
                    can_silently_drop = referenced and len(constraint.fields) == 1
                elif isinstance(constraint, ExclusionConstraint):
                    # A raw SQL term can't be checked for other columns - it never drops silently.
                    referenced = False
                    can_silently_drop = False
                    for field_name, _operator in constraint.expressions:
                        if field_name == self.name:
                            referenced = True
                            can_silently_drop = len(constraint.expressions) == 1
                            break
                        if isinstance(field_name, RawSQLTerm) and self._references_field_name(field_name, self.name):
                            referenced = True
                            can_silently_drop = False
                            break
                elif isinstance(constraint, CheckConstraint):
                    # A raw SQL check isn't a structured field list - can't tell whether it names
                    # any OTHER still-present column too, so it never silently drops; a Q check
                    # reading only this field does, like the single-field cases above.
                    referenced = self._references_field_name(constraint.check, self.name)
                    can_silently_drop = isinstance(constraint.check, Q) and (
                        constraint.check.get_referenced_field_names() == {self.name}
                    )
                else:
                    referenced = False
                    can_silently_drop = False
                if (
                    not referenced
                    and isinstance(constraint, (UniqueConstraint, ExclusionConstraint))
                    and constraint.condition is not None
                    and self._references_field_name(constraint.condition, self.name)
                ):
                    # A partial index's condition is raw SQL - the same.
                    referenced = True
                    can_silently_drop = False
                if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)) and self.name in constraint.include:
                    referenced = True
                    can_silently_drop = False
                if referenced:
                    if not can_silently_drop:
                        constraint_label = f"{constraint.name!r}" if constraint.name else repr(constraint)
                        raise ConfigurationError(
                            f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                            f"still referenced by constraint {constraint_label}. Remove that constraint first."
                        )
                    continue
                remaining_constraints.append(constraint)
            if len(remaining_constraints) != len(constraints):
                model_state.set_option_list(ModelOption.CONSTRAINTS, remaining_constraints)

        triggers = model_state.get_option_list(ModelOption.TRIGGERS)
        if triggers:
            for trigger in triggers:
                # A trigger's body, condition and `on` are raw SQL - the same.
                if (
                    self._references_field_name(trigger.body, self.name)
                    or (trigger.when is not None and self._references_field_name(trigger.when, self.name))
                    or self._references_field_name(trigger.on, self.name)
                ):
                    raise ConfigurationError(
                        f"Can't remove field {self.name!r} from {app_label}.{self.model_name} - it's "
                        f"still referenced by trigger {trigger.name!r}. Remove that trigger first."
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
        await self._remove_field_from_db(old_state, app_label, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self._add_field_to_db(new_state, app_label, state_editor)
