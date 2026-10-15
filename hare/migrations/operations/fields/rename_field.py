from __future__ import annotations

import re
from collections.abc import Iterator
from copy import copy, deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields import Field
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import (
    SELECTED_COLUMN_END_RE,
    SELECTED_COLUMN_START_RE,
    VIEW_QUERY_COLUMN_PATTERN_TEMPLATE,
)
from hare.migrations.operations.fields.expression_field_renaming import ExpressionFieldRenaming
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.operation import Operation
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model
from hare.models.enums import ModelOption
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
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

    def describe(self) -> str:
        return f"Rename field {self.old_name} to {self.new_name} on {self.model_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        if self.new_name in model_state.fields:
            raise IncompatibleStateError(
                f"Field {self.new_name} already present on model {app_label}.{self.model_name}"
            )

        old_field = model_state.fields.pop(self.old_name, None)
        if not old_field:
            raise IncompatibleStateError(
                f"Field {self.old_name} is not present on model {app_label}.{self.model_name}"
            )

        # The given field keeps its real source_field; a hand-written migration without field=
        # carries the old field over.
        new_field = cast("Field[Any]", deepcopy(self.field if self.field is not None else old_field))
        model_state.set_field(self.new_name, new_field)
        old_column = ModelState.get_field_db_column(self.old_name, old_field)
        new_column = ModelState.get_field_db_column(self.new_name, new_field)
        # Raw SQL (check expressions, trigger bodies, index/constraint predicates) names DB
        # columns, not attributes - it only changes when the column itself is renamed.
        renamed_column = (old_column, new_column) if old_column and new_column and old_column != new_column else None
        self._rename_meta_field_references(model_state, renamed_column)
        models_to_reload = self.get_models_to_reload(app_label, state, old_field)
        models_to_reload |= self._rename_referencing_to_fields(app_label, state, old_field)
        models_to_reload |= self._rename_relation_key_field_references(app_label, state)
        models_to_reload |= self._rename_key_field_readers(app_label, state, model_state)

        state.reload_models(models_to_reload)

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
            key_field_rename._rename_meta_field_references(model_state, (key_field_name, renamed_key_field_name))
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
                    key_field_rename._rename_meta_field_references(
                        model_state, (key_field_name, renamed_key_field_name)
                    )
                    changed_model_keys.add(model_key)
                    changed_model_keys |= key_field_rename._rename_referencing_to_fields(
                        model_key[0], state, rendered_model._meta.fields_map[key_field_name]
                    )
                    changed_model_keys |= key_field_rename._rename_relation_key_field_references(model_key[0], state)
        return changed_model_keys

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
                if state.apps.split_reference(field.model_name) != (app_label, self.model_name):
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
        name, the fields computed from it and the model's relations to itself through it rewritten.

        Args:
            fields: The (name, field) pairs of the renamed field's model.
            app_label: The migration's app.

        Returns:
            The pairs after the rename; None when a relation of the model reads the field as one
            of several - its key fields, named after the fields it reads, are renamed with it.
        """
        old_field = cast("Any", dict(fields)[self.old_name])
        new_field = cast("Any", deepcopy(self.field if self.field is not None else old_field))
        old_column = ModelState.get_field_db_column(self.old_name, old_field)
        new_column = ModelState.get_field_db_column(self.new_name, new_field)
        renamed_column = (old_column, new_column) if old_column and new_column and old_column != new_column else None
        renamed_fields: list[tuple[str, FieldLike]] = []
        for field_name, field in fields:
            if field_name == self.old_name:
                renamed_fields.append((self.new_name, new_field))
                continue
            renamed_field: Any = field
            if isinstance(field, Field):
                renamed_field = self._renamed_dependent_field(field, renamed_column)
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

    def _rename_meta_field_references(self, model_state: ModelState, renamed_column: tuple[str, str] | None) -> None:
        """Renames the field in the ``Meta`` options naming it - primary key, indexes, constraints,
        tenant/soft-delete/lock fields, table options, triggers, views, policies, sequences.

        Args:
            model_state: The model state the field is renamed in.
            renamed_column: The (old, new) column pair when the column changes, else None.
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
                self._renamed_index(index, renamed_column) if isinstance(index, Index) else self._renamed_names(index)
                for index in indexes
            ]
            model_state.set_option_list(ModelOption.INDEXES, renamed_indexes)

        constraints = model_state.get_option_list(ModelOption.CONSTRAINTS)
        if constraints:
            model_state.set_option_list(
                ModelOption.CONSTRAINTS,
                [self._renamed_constraint(constraint, renamed_column) for constraint in constraints],
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
                [
                    options.with_renamed_field(
                        self.old_name, self.new_name, lambda term: self._renamed_raw_sql_term(term, renamed_column)
                    )
                    for options in table_options
                ],
            )

        triggers = model_state.get_option_list(ModelOption.TRIGGERS)
        if triggers:
            model_state.set_option_list(
                ModelOption.TRIGGERS, [self._renamed_trigger(trigger, renamed_column) for trigger in triggers]
            )

        for view_option in (ModelOption.VIEWS, ModelOption.MATERIALIZED_VIEWS):
            views = model_state.get_option_list(view_option)
            if views:
                model_state.set_option_list(view_option, [self._renamed_view(view, renamed_column) for view in views])

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
            model_state.set_option_list(
                ModelOption.POLICIES, [self._renamed_policy(policy, renamed_column) for policy in policies]
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
            renamed_dependent_field = self._renamed_dependent_field(field, renamed_column)
            if renamed_dependent_field is not field:
                model_state.set_field(field_name, renamed_dependent_field)

    def _renamed_dependent_field(self, field: Field[Any], renamed_column: tuple[str, str] | None) -> Field[Any]:
        """A copy of a field computed from other columns, with its reference to the renamed field
        rewritten.

        Args:
            field: A field of the model the rename happens on.
            renamed_column: The (old, new) DB column pair when the column changes, else None.

        Returns:
            The rewritten copy, or `field` itself when it doesn't reference the renamed field.
        """
        if isinstance(field, GeneratedField):
            if isinstance(field.expression, dict):
                renamed_expressions = {
                    dialect: self._renamed_raw_sql_term(expression, renamed_column)
                    for dialect, expression in field.expression.items()
                }
                if all(expression is None for expression in renamed_expressions.values()):
                    return field
                renamed_field = copy(field)
                renamed_field.expression = {
                    dialect: renamed_expression if renamed_expression is not None else field.expression[dialect]
                    for dialect, renamed_expression in renamed_expressions.items()
                }
                return renamed_field
            renamed_expression = self._renamed_raw_sql_term(field.expression, renamed_column)
            if renamed_expression is None:
                return field
            renamed_field = copy(field)
            renamed_field.expression = renamed_expression
            return renamed_field
        return field.with_renamed_generated_from_field(self.old_name, self.new_name)

    @classmethod
    def _renamed_raw_sql(cls, sql: str, renamed_column: tuple[str, str] | None) -> str | None:
        """Raw SQL with every whole-identifier occurrence of the old column replaced.

        Args:
            sql: The raw SQL fragment.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten SQL, or None when it doesn't reference the old column.
        """
        if renamed_column is None:
            return None
        old_column, new_column = renamed_column
        if not cls._references_field_name(sql, old_column):
            return None
        return re.sub(rf"\b{re.escape(old_column)}\b", new_column, sql)

    @classmethod
    def _renamed_raw_sql_term(cls, term: RawSQLTerm, renamed_column: tuple[str, str] | None) -> RawSQLTerm | None:
        """A ``RawSQLTerm`` with every whole-identifier occurrence of the old column replaced.

        Args:
            term: The raw SQL.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten term, or None when it doesn't reference the old column.
        """
        renamed_sql = cls._renamed_raw_sql(term.sql, renamed_column)
        return None if renamed_sql is None else RawSQLTerm(renamed_sql)

    def _renamed_condition(self, condition: Any, renamed_column: tuple[str, str] | None) -> Any:
        """A constraint's or partial index's condition reading the renamed field - a ``Q`` by its
        field names, raw SQL by its column.

        Args:
            condition: The condition.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten condition, or None when it doesn't read the renamed field.
        """
        if isinstance(condition, (Q, ExclusiveArcCondition)):
            if self.old_name not in condition.get_referenced_field_names():
                return None
            return condition.with_renamed_field(self.old_name, self.new_name)
        return self._renamed_raw_sql_term(condition, renamed_column)

    @classmethod
    def _renamed_view_query_sql(
        cls, sql: str, renamed_column: tuple[str, str], *, keeps_output_names: bool
    ) -> tuple[str, bool] | None:
        """A view's query with the old column replaced where it is read. An ``AS`` naming an output
        after the old column stays as it is; where the outputs keep their names - the view writes
        its rows into another table - a column selected as it is keeps its too (``new AS old``).

        Args:
            sql: The query.
            renamed_column: The (old, new) DB column pair.
            keeps_output_names: Whether the query's outputs keep their names.

        Returns:
            The rewritten query and whether an output of it still has the old column's name; None
            when the query doesn't name the old column.
        """
        old_column, new_column = renamed_column
        if not cls._references_field_name(sql, old_column):
            return None
        has_old_output = False

        def get_replacement(match: re.Match[str]) -> str:
            nonlocal has_old_output
            if match.group("alias"):
                has_old_output = True
                return match.group(0)
            quote = match.group("quote")
            renamed_sql = f"{match.group('qualifier')}{quote}{new_column}{quote}"
            before = sql[: match.start()].rstrip()
            if (
                keeps_output_names
                and before.count("(") == before.count(")")
                and SELECTED_COLUMN_START_RE.search(before)
                and SELECTED_COLUMN_END_RE.match(sql[match.end() :].lstrip())
            ):
                has_old_output = True
                return f"{renamed_sql} AS {quote}{old_column}{quote}"
            return renamed_sql

        pattern = re.compile(VIEW_QUERY_COLUMN_PATTERN_TEMPLATE.format(column=re.escape(old_column)))
        return pattern.sub(get_replacement, sql), has_old_output

    def _renamed_view(self, view: Any, renamed_column: tuple[str, str] | None) -> Any:
        """A view of the model reading the renamed column - its raw SQL query rewritten
        (``_renamed_view_query_sql()``), a materialized view's unique column, and the columns its
        own table is sorted and partitioned by, named after it renamed with it - the rewritten query
        selects the column under its new name - unless an output of the query keeps the old one.

        Args:
            view: A view or materialized view, its query raw SQL as the state keeps it.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten view, or `view` itself when it doesn't read the column.
        """
        if renamed_column is None or not isinstance(view.query, RawSQLTerm):
            return view
        changes: dict[str, Any] = {}
        renamed_query = self._renamed_view_query_sql(
            view.query.sql, renamed_column, keeps_output_names=getattr(view, "to", None) is not None
        )
        if renamed_query is not None:
            changes["query"] = RawSQLTerm(renamed_query[0])
            if renamed_query[1]:
                # The view's own columns keep their names.
                return replace(view, **changes)
        unique_columns = getattr(view, "unique_columns", ())
        if renamed_column[0] in unique_columns:
            changes["unique_columns"] = tuple(
                renamed_column[1] if column == renamed_column[0] else column for column in unique_columns
            )
        order_by = getattr(view, "order_by", ())
        renamed_order_by = tuple(
            (renamed_column[1] if key == renamed_column[0] else key)
            if isinstance(key, str)
            else (self._renamed_raw_sql_term(key, renamed_column) or key)
            for key in order_by
        )
        if renamed_order_by != tuple(order_by):
            changes["order_by"] = renamed_order_by
        partition_by = getattr(view, "partition_by", None)
        if isinstance(partition_by, RawSQLTerm) and (
            renamed_partition_by := self._renamed_raw_sql_term(partition_by, renamed_column)
        ):
            changes["partition_by"] = renamed_partition_by
        return replace(view, **changes) if changes else view

    def _renamed_policy(self, policy: Any, renamed_column: tuple[str, str] | None) -> Any:
        """A row level security policy reading the renamed field - its ``using`` and ``with_check``
        conditions, a ``Q`` by its field names, raw SQL by its column.

        Args:
            policy: The policy.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten policy, or `policy` itself when it doesn't read the field.
        """
        changes: dict[str, Any] = {}
        for attribute_name in ("using", "with_check"):
            condition = getattr(policy, attribute_name)
            if not isinstance(condition, (Q, RawSQLTerm)):
                continue
            if (renamed_condition := self._renamed_condition(condition, renamed_column)) is not None:
                changes[attribute_name] = renamed_condition
        return replace(policy, **changes) if changes else policy

    def _renamed_trigger(self, trigger: Any, renamed_column: tuple[str, str] | None) -> Any:
        """A trigger reading the renamed field - its body and ``on`` are raw procedural SQL, given the
        same word-boundary rewrite ``_renamed_constraint`` gives raw SQL; its condition is a ``Q`` by
        its field names or raw SQL by its column."""
        changes: dict[str, Any] = {}
        if (renamed_body := self._renamed_raw_sql_term(trigger.body, renamed_column)) is not None:
            changes["body"] = renamed_body
        if (
            trigger.when is not None
            and (renamed_when := self._renamed_condition(trigger.when, renamed_column)) is not None
        ):
            changes["when"] = renamed_when
        if (renamed_on := self._renamed_raw_sql(trigger.on, renamed_column)) is not None:
            changes["on"] = renamed_on
        return replace(trigger, **changes) if changes else trigger

    def _renamed_index(self, index: Index, renamed_column: tuple[str, str] | None) -> Index:
        """An index with the field renamed in its fields, its key expressions and, for a partial
        index, its condition - a copy.
        """
        fields_changed = self.old_name in index.fields or self.old_name in index.include
        new_condition = (
            self._renamed_condition(index.condition, renamed_column)
            if isinstance(index, PartialIndex) and index.condition is not None
            else None
        )
        condition_changed = new_condition is not None
        renamed_expressions = [
            self._renamed_index_key(expression, renamed_column) for expression in index.declared_expressions
        ]
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

    def _renamed_index_key(self, key: Any, renamed_column: tuple[str, str] | None) -> Any:
        """An index's key expression with the renamed field rewritten - raw SQL by its column, an
        expression or ordering by its field names.

        Args:
            key: The key as declared.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten key, or ``key`` itself when it doesn't read the renamed field.
        """
        if isinstance(key, RawSQLTerm):
            renamed_term = self._renamed_raw_sql_term(key, renamed_column)
            return key if renamed_term is None else renamed_term
        if isinstance(key, OrderedIndexKey):
            renamed_key_term = self._renamed_index_key(key.term, renamed_column)
            return key if renamed_key_term is key.term else OrderedIndexKey(renamed_key_term, key.order)
        return ExpressionFieldRenaming.get_renamed(key, self.old_name, self.new_name)

    def _renamed_constraint(self, constraint: Any, renamed_column: tuple[str, str] | None) -> Any:
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
            renamed_expressions: list[tuple[str | RawSQLTerm, str]] = []
            for field_name, operator in constraint.expressions:
                if isinstance(field_name, RawSQLTerm):
                    renamed_term = self._renamed_raw_sql_term(field_name, renamed_column)
                    if renamed_term is not None:
                        renamed_expressions.append((renamed_term, operator))
                        expressions_changed = True
                        continue
                elif field_name == self.old_name:
                    renamed_expressions.append((self.new_name, operator))
                    expressions_changed = True
                    continue
                renamed_expressions.append((field_name, operator))
            if expressions_changed:
                changes["expressions"] = tuple(renamed_expressions)

        if (
            isinstance(constraint, CheckConstraint)
            and (renamed_check := self._renamed_condition(constraint.check, renamed_column)) is not None
        ):
            changes["check"] = renamed_check

        if (
            isinstance(constraint, (UniqueConstraint, ExclusionConstraint))
            and constraint.condition is not None
            and (renamed_condition := self._renamed_condition(constraint.condition, renamed_column)) is not None
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
            await self._recreate_rewritten_triggers(old_model, new_model, state_editor)
            await state_editor.views.alter_changed_views(old_model, new_model)
            await state_editor.materialized_views.alter_changed_materialized_views(old_model, new_model)
            await state_editor.index_statements.recreate_indexes_reading_renamed_columns(old_model, new_model)
            if not remade:
                renamed_models.append((old_model, new_model))
        await self._rename_through_table_columns(old_state, new_state, state_editor)
        # An unnamed index named after its keys - a column or an expression reading it - takes the
        # name the renamed model gives it.
        for old_model, new_model in renamed_models:
            await state_editor.generated_index_names.rename_generated_index_names(old_model, new_model)

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
        if not isinstance(old_renamed_field, ManyToManyFieldInstance) and self._get_db_column_names(
            old_renamed_field
        ) != self._get_db_column_names(new_renamed_field):
            field_pairs_by_model[new_renamed_model] = (old_renamed_model, [(old_renamed_field, new_renamed_field)])
        for old_model, new_model, old_field, new_field in self._get_fields_of_both_states(old_state, new_state):
            if (
                isinstance(new_field, ForeignKeyFieldInstance)
                and isinstance(old_field, ForeignKeyFieldInstance)
                and self._get_db_column_names(old_field) != self._get_db_column_names(new_field)
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
                # Its key indexes are named after the columns.
                await state_editor.many_to_many_through_tables.alter_many_to_many_through_indexes(
                    new_model._meta.schema, old_field, new_field
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

    @staticmethod
    def _get_db_column_names(field: Field[Any]) -> tuple[str, ...]:
        """The DB columns a resolved field occupies.

        Args:
            field: A field from a rendered state model.

        Returns:
            The column names, one per key column for a FK/O2O.
        """
        if isinstance(field, ForeignKeyFieldInstance) and field.db_column_names:
            return field.db_column_names
        return (field.source_field or field.model_field_name,)

    @staticmethod
    async def _recreate_rewritten_triggers(
        old_model: type[Model], new_model: type[Model], state_editor: BaseSchemaEditor
    ) -> None:
        """Recreates every trigger whose SQL the rename rewrote - a Postgres trigger function
        body is stored as text and keeps naming the old column after RENAME COLUMN.

        Args:
            old_model: The model before the rename.
            new_model: The model after the rename.
            state_editor: The schema editor.
        """
        old_triggers_by_name = {trigger.name: trigger for trigger in old_model._meta.triggers}
        for new_trigger in new_model._meta.triggers:
            old_trigger = old_triggers_by_name.get(new_trigger.name)
            if old_trigger is None or old_trigger == new_trigger:
                continue
            await state_editor.trigger_statements.alter_trigger(new_model, old_trigger, new_trigger)

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
