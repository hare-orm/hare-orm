from __future__ import annotations

from collections.abc import Iterator
from copy import copy, deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.schema.columns.column_renames import ColumnRenames
from hare.fields import Field
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.models.enums import ModelOption
from hare.query.expressions import Q

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class RenameField(ModelBoundOperation):
    """Renames a model's field and its column."""

    def __init__(self, model_name: str, old_name: str, new_name: str, field: FieldLike | None = None) -> None:
        self.model_name = model_name
        self.old_name = old_name
        self.new_name = new_name
        self.field = field

    def get_fields(self) -> list[Any]:
        """The renamed field, when the operation carries it."""
        return [] if self.field is None else [self.field]

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """Renames of a field in a row become one rename - none when it ends at the first name."""
        if (
            isinstance(other, RenameField)
            and self.is_same_model_name(self.model_name, other.model_name)
            and other.old_name == self.new_name
        ):
            if other.new_name == self.old_name:
                return []
            return [RenameField(self.model_name, self.old_name, other.new_name, other.field or self.field)]
        return super().reduce(other, app_label)

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [
            OperationDependency.of_field(app_label, self.model_name, self.old_name, DependencyType.REMOVED),
            OperationDependency.of_field(app_label, self.model_name, self.new_name, DependencyType.CREATED),
        ]

    def describe(self) -> str:
        return f"Rename field {self.old_name} to {self.new_name} on {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.new_name in model_state.fields:
            raise IncompatibleStateError(
                f"Field {self.new_name} already present on model {app_label}.{self.model_name}"
            )

        old_field = model_state.fields.get(self.old_name)
        if not old_field:
            raise IncompatibleStateError(
                f"Field {self.old_name} is not present on model {app_label}.{self.model_name}"
            )
        if isinstance(old_field, ForeignKeyFieldInstance) or self._may_be_related_to(app_label, state):
            # The composite key relations renamed below are read from the models as rendered
            # before the rename - rendered now, while the model states are still unchanged.
            _ = state.apps
        self.rename_in_model_state(model_state)
        changed_model_keys = self.get_changed_model_keys(app_label, old_field)
        changed_model_keys |= self._rename_referencing_to_fields(app_label, state, old_field)
        changed_model_keys |= self._rename_relation_key_field_references(app_label, state)
        if isinstance(old_field, ForeignKeyFieldInstance):
            changed_model_keys |= self._rename_key_field_readers(app_label, state, model_state)

        state.mark_models_changed(changed_model_keys)

    def rename_in_model_state(self, model_state: ModelState) -> None:
        """Renames the field in its model's state - the field and what the model's options name it by;
        raw SQL naming the column is the user's and is never rewritten.

        Args:
            model_state: The field's model state.
        """
        old_field = model_state.fields.pop(self.old_name)
        # The given field keeps its real source_field; a hand-written migration without field=
        # carries the old field over.
        new_field = cast("Field[Any]", deepcopy(self.field if self.field is not None else old_field))
        model_state.set_field(self.new_name, new_field)
        self._rename_meta_field_references(model_state)

    def _rename_key_field_readers(self, app_label: str, state: State, model_state: ModelState) -> set[tuple[str, str]]:
        """A renamed relation to a composite key renames its key fields (``<relation>_<field>``) - the
        relations reading them are pointed at the new names: a ``to_field`` naming one, and their own
        key fields named after it.

        Args:
            app_label: The migration's app label.
            state: The state the rename is applied to, its models not rendered again yet.
            model_state: The renamed field's model state.

        Returns:
            The (app_label, model_name) keys of the models whose relations or options changed.
        """
        try:
            rendered_model = state.apps.get_model(app_label, self.model_name)
        except KeyError:
            return set()
        relation = rendered_model._meta.fields_map.get(self.old_name)
        if not isinstance(relation, ForeignKeyFieldInstance) or len(relation.source_fields) < 2:
            return set()
        changed_model_keys: set[tuple[str, str]] = set()
        for key_field_name in relation.source_fields:
            renamed_key_field_name = f"{self.new_name}{key_field_name.removeprefix(self.old_name)}"
            key_field_rename = RenameField(self.model_name, key_field_name, renamed_key_field_name)
            key_field_rename._rename_meta_field_references(model_state)
            changed_model_keys |= key_field_rename._rename_referencing_to_fields(
                app_label, state, rendered_model._meta.fields_map[key_field_name]
            )
            changed_model_keys |= key_field_rename._rename_relation_key_field_references(app_label, state)
        return changed_model_keys

    def _rename_relation_key_field_references(self, app_label: str, state: State) -> set[tuple[str, str]]:
        """Renames, in the Meta options of each model relating to the renamed field's model through a
        composite key, the key field named after the renamed field (``<relation>_<field>``) - a
        one-to-one relation's uniqueness, an index over the relation's keys - and, as that key field
        is renamed too, the relations reading it in turn: a ``to_field`` naming it, and their own key
        fields named after it.

        Args:
            app_label: The migration's app label.
            state: The state the rename is applied to, its models not rendered again yet.

        Returns:
            The (app_label, model_name) keys of the models whose options changed.
        """
        if not self._may_be_related_to(app_label, state):
            return set()
        try:
            target_model = state.apps.get_model(app_label, self.model_name)
        except KeyError:
            return set()
        changed_model_keys: set[tuple[str, str]] = set()
        for model_key, model_state in state.models.items():
            try:
                rendered_model = state.apps.get_model(*model_key)
            except KeyError:
                continue
            for relation in rendered_model._meta.fields_map.values():
                if (
                    not isinstance(relation, ForeignKeyFieldInstance)
                    or len(relation.source_fields) < 2
                    or relation.related_model is not target_model
                ):
                    continue
                for key_field_name, to_field in zip(relation.source_fields, relation.to_field_instances, strict=True):
                    if to_field.model_field_name != self.old_name:
                        continue
                    renamed_key_field_name = f"{relation.model_field_name}_{self.new_name}"
                    key_field_rename = RenameField(model_key[1], key_field_name, renamed_key_field_name)
                    key_field_rename._rename_meta_field_references(model_state)
                    changed_model_keys.add(model_key)
                    changed_model_keys |= key_field_rename._rename_referencing_to_fields(
                        model_key[0], state, rendered_model._meta.fields_map[key_field_name]
                    )
                    changed_model_keys |= key_field_rename._rename_relation_key_field_references(model_key[0], state)
        return changed_model_keys

    def _may_be_related_to(self, app_label: str, state: State) -> bool:
        """Whether a relation of the state may point at the renamed field's model - read from the
        model states, so a state no relation of which does isn't rendered for the rename.

        Args:
            app_label: The migration's app label.
            state: The state the rename is applied to.

        Returns:
            True for a relation naming the model, or naming a model by its class.
        """
        model_reference = f"{app_label}.{self.model_name}"
        for model_state in state.models.values():
            for field in model_state.fields.values():
                if not isinstance(field, ForeignKeyFieldInstance):
                    continue
                reference = SwappableModelReference.get_model_reference(field.model_name)
                if not isinstance(reference, str) or reference == model_reference:
                    return True
        return False

    def _rename_referencing_to_fields(
        self, app_label: str, state: State, old_field: Field[Any]
    ) -> set[tuple[str, str]]:
        """Points every relation whose ``to_field`` names the renamed field at its new name.

        Args:
            app_label: The migration's app label.
            state: The state the rename is applied to.
            old_field: The renamed field's previous definition.

        Returns:
            The (app_label, model_name) keys of the models whose relations were updated.
        """
        referencing_model_keys: set[tuple[str, str]] = set()
        for model_key, referencing_model_state in state.models.items():
            for field_name, field in list(referencing_model_state.fields.items()):
                if not isinstance(field, ForeignKeyFieldInstance) or field.to_field is None:
                    continue
                if StateApps.split_reference(field.model_name) != (app_label, self.model_name):
                    continue
                renamed_field = self._get_relation_reading_new_name(field, old_field)
                if renamed_field is not field:
                    referencing_model_state.set_field(field_name, renamed_field)
                    referencing_model_keys.add(model_key)
        return referencing_model_keys

    def _get_relation_reading_new_name(self, relation: Any, old_field: Any) -> Any:
        """A relation to the renamed field's model with the renamed field in its ``to_field``
        under the new name.

        Args:
            relation: A relation to the renamed field's model.
            old_field: The renamed field's previous definition.

        Returns:
            The rewritten copy; the relation itself when it doesn't read the renamed field.
        """
        if relation.to_field is None:
            return relation
        new_names_by_old_name = {self.old_name: self.new_name}
        if isinstance(old_field, ForeignKeyFieldInstance):
            # A relation targeting a OneToOneField primary key names that field's shadow attribute.
            new_names_by_old_name[f"{self.old_name}{FOREIGN_KEY_COLUMN_SUFFIX}"] = (
                f"{self.new_name}{FOREIGN_KEY_COLUMN_SUFFIX}"
            )
        to_field_names = relation.to_field if isinstance(relation.to_field, tuple) else (relation.to_field,)
        renamed_to_field_names = tuple(new_names_by_old_name.get(name, name) for name in to_field_names)
        if renamed_to_field_names == to_field_names:
            return relation
        renamed_relation = copy(relation)
        renamed_relation.to_field = (
            renamed_to_field_names if isinstance(relation.to_field, tuple) else renamed_to_field_names[0]
        )
        return renamed_relation

    def get_renamed_model_fields(
        self, fields: list[tuple[str, FieldLike]], app_label: str
    ) -> list[tuple[str, FieldLike]] | None:
        """The fields a model is created with, as the rename leaves them: the field under its new
        name, the fields generated from it and the model's relations to itself through it renamed.

        Args:
            fields: The (name, field) pairs of the renamed field's model.
            app_label: The migration's app.

        Returns:
            The pairs after the rename; None when a relation of the model reads the field as one
            of several - its key fields, named after the fields it reads, are renamed with it.
        """
        old_field = cast("Any", dict(fields)[self.old_name])
        new_field = cast("Any", deepcopy(self.field if self.field is not None else old_field))
        renamed_fields: list[tuple[str, FieldLike]] = []
        for field_name, field in fields:
            if field_name == self.old_name:
                renamed_fields.append((self.new_name, new_field))
                continue
            renamed_field: Any = field
            if isinstance(field, Field):
                renamed_field = field.with_renamed_generated_from_field(self.old_name, self.new_name)
            if (
                isinstance(renamed_field, ForeignKeyFieldInstance)
                and SwappableModelReference.get_model_reference(renamed_field.model_name)
                == f"{app_label}.{self.model_name}"
            ):
                relation = self._get_relation_reading_new_name(renamed_field, old_field)
                if relation is not renamed_field and isinstance(relation.to_field, tuple):
                    return None
                renamed_field = relation
            renamed_fields.append((field_name, renamed_field))
        return renamed_fields

    def _renamed_names(self, names: list[str] | tuple[str, ...]) -> Any:
        """Same sequence, same type (list stays list, tuple stays tuple), with every
        occurrence of `self.old_name` replaced by `self.new_name`."""
        return type(names)(self.new_name if name == self.old_name else name for name in names)

    def _rename_meta_field_references(self, model_state: ModelState) -> None:
        """Renames the field where the ``Meta`` options name it - the primary key, the fields and hare
        expressions of indexes and constraints, ``Q`` conditions, the tenant/soft-delete/lock fields,
        table options, a dictionary's fields, the columns of a grant, a sequence's owner. Raw SQL is left
        as the user wrote it.

        Args:
            model_state: The model state the field is renamed in.
        """
        if model_state.pk_field_name == self.old_name:
            model_state.pk_field_name = self.new_name
        elif isinstance(model_state.pk_field_name, tuple):
            model_state.pk_field_name = self._renamed_names(model_state.pk_field_name)

        primary_key_attribute = model_state.options.get(ModelOption.PRIMARY_KEY_ATTRIBUTE)
        if primary_key_attribute == self.old_name:
            model_state.options[ModelOption.PRIMARY_KEY_ATTRIBUTE] = self.new_name
        elif isinstance(primary_key_attribute, tuple):
            model_state.options[ModelOption.PRIMARY_KEY_ATTRIBUTE] = self._renamed_names(primary_key_attribute)

        indexes = model_state.get_option_list(ModelOption.INDEXES)
        if indexes:
            renamed_indexes = [
                self._renamed_index(index) if isinstance(index, Index) else self._renamed_names(index)
                for index in indexes
            ]
            model_state.set_option_list(ModelOption.INDEXES, renamed_indexes)

        constraints = model_state.get_option_list(ModelOption.CONSTRAINTS)
        if constraints:
            model_state.set_option_list(
                ModelOption.CONSTRAINTS,
                [self._renamed_constraint(constraint) for constraint in constraints],
            )

        for field_option in (
            ModelOption.TENANT_FIELD,
            ModelOption.SOFT_DELETE_FIELD,
            ModelOption.OPTIMISTIC_LOCK_FIELD,
        ):
            if model_state.options.get(field_option) == self.old_name:
                model_state.options[field_option] = self.new_name

        table_options = model_state.get_option_list(ModelOption.TABLE_OPTIONS)
        if table_options:
            model_state.set_option_list(
                ModelOption.TABLE_OPTIONS,
                [options.with_renamed_field(self.old_name, self.new_name) for options in table_options],
            )

        triggers = model_state.get_option_list(ModelOption.TRIGGERS)
        if triggers:
            model_state.set_option_list(ModelOption.TRIGGERS, [self._renamed_trigger(trigger) for trigger in triggers])

        dictionaries = model_state.get_option_list(ModelOption.DICTIONARIES)
        if dictionaries:
            model_state.set_option_list(
                ModelOption.DICTIONARIES,
                [
                    replace(
                        dictionary,
                        key=tuple(self._renamed_names(dictionary.key)),
                        attributes=tuple(self._renamed_names(dictionary.attributes)),
                    )
                    if self.old_name in dictionary.get_field_names()
                    else dictionary
                    for dictionary in dictionaries
                ],
            )

        policies = model_state.get_option_list(ModelOption.POLICIES)
        if policies:
            model_state.set_option_list(ModelOption.POLICIES, [self._renamed_policy(policy) for policy in policies])

        grants = model_state.get_option_list(ModelOption.GRANTS)
        if grants:
            model_state.set_option_list(
                ModelOption.GRANTS,
                [
                    replace(grant, columns=self._renamed_names(grant.columns))
                    if self.old_name in grant.columns
                    else grant
                    for grant in grants
                ],
            )

        sequences = model_state.get_option_list(ModelOption.SEQUENCES)
        if sequences:
            model_state.set_option_list(
                ModelOption.SEQUENCES,
                [
                    replace(sequence, owned_by=self.new_name) if sequence.owned_by == self.old_name else sequence
                    for sequence in sequences
                ],
            )

        for field_name, field in list(model_state.fields.items()):
            renamed_dependent_field = field.with_renamed_generated_from_field(self.old_name, self.new_name)
            if renamed_dependent_field is not field:
                model_state.set_field(field_name, renamed_dependent_field)

    def _renamed_condition(self, condition: Any) -> Any:
        """A condition reading the renamed field - a ``Q`` by its field names; raw SQL is left as it is.

        Args:
            condition: The condition.

        Returns:
            The renamed condition, or None when it names no renamed field.
        """
        if (
            isinstance(condition, (Q, ExclusiveArcCondition))
            and self.old_name in condition.get_referenced_field_names()
        ):
            return condition.with_renamed_field(self.old_name, self.new_name)
        return None

    def _renamed_policy(self, policy: Any) -> Any:
        """A row level security policy whose ``Q`` conditions read the renamed field.

        Args:
            policy: The policy.

        Returns:
            The renamed policy, or `policy` itself when no condition of it names the field.
        """
        changes: dict[str, Any] = {}
        for attribute_name in ("using", "with_check"):
            if (renamed_condition := self._renamed_condition(getattr(policy, attribute_name))) is not None:
                changes[attribute_name] = renamed_condition
        return replace(policy, **changes) if changes else policy

    def _renamed_trigger(self, trigger: Any) -> Any:
        """A trigger whose ``Q`` condition reads the renamed field - its body is raw SQL, left as it is.

        Args:
            trigger: The trigger.

        Returns:
            The renamed trigger, or `trigger` itself when its condition names no renamed field.
        """
        if (renamed_when := self._renamed_condition(trigger.when)) is None:
            return trigger
        return replace(trigger, when=renamed_when)

    def _renamed_index(self, index: Index) -> Index:
        """An index with the field renamed in its fields, its key expressions and, for a partial
        index, its condition - a copy.
        """
        fields_changed = self.old_name in index.fields or self.old_name in index.include
        new_condition = (
            self._renamed_condition(index.condition)
            if isinstance(index, PartialIndex) and index.condition is not None
            else None
        )
        condition_changed = new_condition is not None
        renamed_expressions = [self._renamed_index_key(expression) for expression in index.declared_expressions]
        expressions_changed = any(
            renamed is not expression
            for renamed, expression in zip(renamed_expressions, index.declared_expressions, strict=True)
        )

        if not fields_changed and not condition_changed and not expressions_changed:
            return index

        if condition_changed or expressions_changed:
            # Rebuilt from its constructor arguments rather than patched: a subclass (e.g. an
            # HNSW/IVFFlat index) folds its own WITH (...) parameters into `extra` ahead of the
            # WHERE clause, which only its own __init__ knows how to render; the key expressions
            # are resolved against the model from the declared ones.
            _path, args, kwargs = index.deconstruct()
            args = renamed_expressions
            if condition_changed:
                kwargs["condition"] = new_condition
            if fields_changed:
                if index.fields:
                    kwargs["fields"] = index.get_declared_fields(self._renamed_names(index.fields))
                if index.include:
                    kwargs["include"] = list(self._renamed_names(index.include))
            return type(index)(*args, **kwargs)

        # Shallow copy first so the rename never mutates the original Index in place - it may
        # still be referenced by an already-cloned old State, which must keep seeing the
        # pre-rename field name.
        renamed_index = copy(index)
        renamed_index.fields = self._renamed_names(index.fields)
        renamed_index.include = self._renamed_names(index.include)
        return renamed_index

    def _renamed_index_key(self, key: Any) -> Any:
        """An index's key expression with the renamed field renamed - an expression or ordering by its
        field names; raw SQL is left as it is.

        Args:
            key: The key as declared.

        Returns:
            The renamed key, or ``key`` itself when it names no renamed field.
        """
        if isinstance(key, (RawSQLTerm, OrderedIndexKey)):
            return key
        return ExpressionFieldRenaming.get_renamed(key, self.old_name, self.new_name)

    def _renamed_constraint(self, constraint: Any) -> Any:
        """A constraint with the field renamed in its fields/expressions and its condition - a new
        instance, as constraints are frozen.
        """
        changes: dict[str, Any] = {}

        if isinstance(constraint, (UniqueConstraint, ForeignKeyConstraint)) and self.old_name in constraint.fields:
            changes["fields"] = self._renamed_names(constraint.fields)

        if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)) and self.old_name in constraint.include:
            changes["include"] = tuple(self._renamed_names(constraint.include))

        if isinstance(constraint, ExclusionConstraint):
            expressions_changed = False
            renamed_expressions: list[tuple[Any, str]] = []
            for expression, operator in constraint.expressions:
                if expression == self.old_name:
                    renamed_expressions.append((self.new_name, operator))
                    expressions_changed = True
                    continue
                renamed_expression = ExpressionFieldRenaming.get_renamed(expression, self.old_name, self.new_name)
                expressions_changed = expressions_changed or renamed_expression is not expression
                renamed_expressions.append((renamed_expression, operator))
            if expressions_changed:
                changes["expressions"] = tuple(renamed_expressions)

        if (
            isinstance(constraint, CheckConstraint)
            and (renamed_check := self._renamed_condition(constraint.check)) is not None
        ):
            changes["check"] = renamed_check

        if (
            isinstance(constraint, (UniqueConstraint, ExclusionConstraint))
            and constraint.condition is not None
            and (renamed_condition := self._renamed_condition(constraint.condition)) is not None
        ):
            changes["condition"] = renamed_condition

        return replace(constraint, **changes) if changes else constraint

    async def _rename_columns(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None,
        old_field_name: str,
        new_field_name: str,
    ) -> None:
        """Renames the field's columns and the columns other tables name after it - a relation's to a
        composite key (``<relation>_<field>``), an automatic through table's - then the indexes named
        after them.

        Args:
            app_label: The migration's app label.
            old_state: The state the columns are renamed from.
            new_state: The state they are renamed to.
            state_editor: The schema editor, None to change no database.
            old_field_name: The field's name in ``old_state``.
            new_field_name: Its name in ``new_state``.
        """
        if not state_editor:
            return
        renamed_models = []
        for old_model, new_model, field_pairs in self._get_renamed_column_fields(
            app_label, old_state, new_state, old_field_name, new_field_name
        ):
            await state_editor.index_statements.drop_indexes_reading_columns_to_rename(old_model, new_model)
            remade = await state_editor.column_renames.rename_model_columns(old_model, new_model, field_pairs)
            await state_editor.index_statements.recreate_indexes_reading_renamed_columns(old_model, new_model)
            if not remade:
                renamed_models.append((old_model, new_model, self.get_renamed_field_names(field_pairs)))
        await self._rename_through_table_columns(old_state, new_state, state_editor)
        # An index or a constraint named after its columns - or an expression reading them - takes the
        # name the renamed model gives it.
        for old_model, new_model, renamed_field_names in renamed_models:
            await state_editor.generated_index_names.rename_generated_index_names(
                old_model, new_model, renamed_field_names
            )

    @staticmethod
    def get_renamed_field_names(field_pairs: list[tuple[Field[Any], Field[Any]]]) -> dict[str, str]:
        """The new name of each renamed field - with a relation's key fields.

        Args:
            field_pairs: The (old, new) fields whose columns are renamed.

        Returns:
            The new name by the old one.
        """
        renamed_field_names: dict[str, str] = {}
        for old_field, new_field in field_pairs:
            renamed_field_names[old_field.model_field_name] = new_field.model_field_name
            if isinstance(old_field, ForeignKeyFieldInstance) and isinstance(new_field, ForeignKeyFieldInstance):
                for old_key, new_key in zip(old_field.source_fields, new_field.source_fields, strict=True):
                    renamed_field_names[old_key] = new_key
        return renamed_field_names

    def _get_renamed_column_fields(
        self, app_label: str, old_state: State, new_state: State, old_field_name: str, new_field_name: str
    ) -> list[tuple[type[Model], type[Model], list[tuple[Field[Any], Field[Any]]]]]:
        """The fields whose columns the rename changes, by model - the renamed field, and each relation
        whose key columns are named after it.

        Args:
            app_label: The migration's app label.
            old_state: The state the columns are renamed from.
            new_state: The state they are renamed to.
            old_field_name: The renamed field's name in ``old_state``.
            new_field_name: Its name in ``new_state``.

        Returns:
            Per model: the model in both states and the (old, new) fields whose columns change.
        """
        old_renamed_model = self._model(old_state, app_label)
        new_renamed_model = self._model(new_state, app_label)
        old_renamed_field = old_renamed_model._meta.fields_map[old_field_name]
        new_renamed_field = new_renamed_model._meta.fields_map[new_field_name]
        field_pairs_by_model: dict[type[Model], tuple[type[Model], list[tuple[Field[Any], Field[Any]]]]] = {}
        if not isinstance(old_renamed_field, ManyToManyFieldInstance) and ColumnRenames.get_column_names(
            old_renamed_field
        ) != ColumnRenames.get_column_names(new_renamed_field):
            field_pairs_by_model[new_renamed_model] = (old_renamed_model, [(old_renamed_field, new_renamed_field)])
        for old_model, new_model, old_field, new_field in self._get_fields_of_both_states(old_state, new_state):
            if (
                isinstance(new_field, ForeignKeyFieldInstance)
                and isinstance(old_field, ForeignKeyFieldInstance)
                and ColumnRenames.get_column_names(old_field) != ColumnRenames.get_column_names(new_field)
            ):
                field_pairs_by_model.setdefault(new_model, (old_model, []))[1].append((old_field, new_field))
        return [(old_model, new_model, pairs) for new_model, (old_model, pairs) in field_pairs_by_model.items()]

    @staticmethod
    async def _rename_through_table_columns(
        old_state: State, new_state: State, state_editor: BaseSchemaEditor
    ) -> None:
        """Renames the columns automatic through tables name after the renamed field.

        Args:
            old_state: The state the columns are renamed from.
            new_state: The state they are renamed to.
            state_editor: The schema editor.
        """
        # A through table is reached from both of its relations - renamed through the declared one,
        # whose keys name its index keys as they were created.
        for _old_model, new_model, old_field, new_field in RenameField._get_fields_of_both_states(
            old_state, new_state
        ):
            if (
                isinstance(new_field, ManyToManyFieldInstance)
                and isinstance(old_field, ManyToManyFieldInstance)
                and not new_field._generated
                and new_field.through_model_class is None
                and old_field.through == new_field.through
            ):
                await state_editor.column_renames.rename_table_columns(
                    new_field.through,
                    new_model._meta.schema,
                    (*old_field.forward_keys, *old_field.backward_keys),
                    (*new_field.forward_keys, *new_field.backward_keys),
                )
                # Its key indexes and foreign keys are named after the columns.
                await state_editor.many_to_many_through_tables.alter_many_to_many_through_indexes(
                    new_model._meta.schema, old_field, new_field
                )
                await state_editor.many_to_many_through_tables.rename_many_to_many_through_foreign_keys(
                    state_editor.qualify_table_name(new_field.through, new_model._meta.schema),
                    new_model,
                    old_field,
                    new_field,
                )

    @staticmethod
    def _get_fields_of_both_states(
        old_state: State, new_state: State
    ) -> Iterator[tuple[type[Model], type[Model], Field[Any], Field[Any]]]:
        """Each field present in both states under the same name, with its model in both.

        Args:
            old_state: The state before.
            new_state: The state after.

        Yields:
            The model before and after the change, the field before and after.
        """
        for app_label, new_models in new_state.apps.apps.items():
            old_models = old_state.apps.apps.get(app_label, {})
            for model_name, new_model in new_models.items():
                old_model = old_models.get(model_name)
                if old_model is None:
                    continue
                for field_name, new_field in new_model._meta.fields_map.items():
                    old_field = old_model._meta.fields_map.get(field_name)
                    if old_field is not None:
                        yield old_model, new_model, old_field, new_field

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._rename_columns(app_label, old_state, new_state, state_editor, self.old_name, self.new_name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._rename_columns(app_label, old_state, new_state, state_editor, self.new_name, self.old_name)
