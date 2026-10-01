from __future__ import annotations

import re
from copy import copy, deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields import Field
from hare.fields.constants import FK_COLUMN_SUFFIX
from hare.fields.generated import GeneratedField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.base.model_bound_operation import ModelBoundOperation
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.state.project.state import State
from hare.models import Model
from hare.models.enums import ModelOption
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RenameField(ModelBoundOperation):
    def __init__(self, model_name: str, old_name: str, new_name: str, field: FieldLike | None = None) -> None:
        self.model_name = model_name
        self.old_name = old_name
        self.new_name = new_name
        self.field = field

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
        models_to_reload = {(app_label, self.model_name)}
        if isinstance(old_field, DIRECT_RELATION_FIELDS):
            models_to_reload.add(state.apps.split_reference(old_field.model_name))
        models_to_reload |= self._rename_referencing_to_fields(app_label, state, old_field)

        state.reload_models(models_to_reload)

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
        new_names_by_old_name = {self.old_name: self.new_name}
        if isinstance(old_field, ForeignKeyFieldInstance):
            # A relation targeting a OneToOneField primary key names that field's shadow attribute.
            new_names_by_old_name[f"{self.old_name}{FK_COLUMN_SUFFIX}"] = f"{self.new_name}{FK_COLUMN_SUFFIX}"
        referencing_model_keys: set[tuple[str, str]] = set()
        for model_key, referencing_model_state in state.models.items():
            for field_name, field in list(referencing_model_state.fields.items()):
                if not isinstance(field, ForeignKeyFieldInstance) or field.to_field is None:
                    continue
                if state.apps.split_reference(field.model_name) != (app_label, self.model_name):
                    continue
                to_field_names = field.to_field if isinstance(field.to_field, tuple) else (field.to_field,)
                renamed_to_field_names = tuple(new_names_by_old_name.get(name, name) for name in to_field_names)
                if renamed_to_field_names == to_field_names:
                    continue
                renamed_field = copy(field)
                renamed_field.to_field = (
                    renamed_to_field_names if isinstance(field.to_field, tuple) else renamed_to_field_names[0]
                )
                referencing_model_state.set_field(field_name, renamed_field)
                referencing_model_keys.add(model_key)
        return referencing_model_keys

    def _renamed_names(self, names: list[str] | tuple[str, ...]) -> Any:
        """Same sequence, same type (list stays list, tuple stays tuple), with every
        occurrence of `self.old_name` replaced by `self.new_name`."""
        return type(names)(self.new_name if name == self.old_name else name for name in names)

    def _rename_meta_field_references(self, model_state: ModelState, renamed_column: tuple[str, str] | None) -> None:
        """Renames the field in the ``Meta`` options naming it - primary key, indexes, constraints.

        Args:
            model_state: The model state the field is renamed in.
            renamed_column: The (old, new) column pair when the column changes, else None.
        """
        if model_state.pk_field_name == self.old_name:
            model_state.pk_field_name = self.new_name
        elif isinstance(model_state.pk_field_name, tuple):
            model_state.pk_field_name = self._renamed_names(model_state.pk_field_name)

        pk_attr = model_state.options.get(ModelOption.PK_ATTR)
        if pk_attr == self.old_name:
            model_state.options[ModelOption.PK_ATTR] = self.new_name
        elif isinstance(pk_attr, tuple):
            model_state.options[ModelOption.PK_ATTR] = self._renamed_names(pk_attr)

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

        triggers = model_state.get_option_list(ModelOption.TRIGGERS)
        if triggers:
            model_state.set_option_list(
                ModelOption.TRIGGERS, [self._renamed_trigger(trigger, renamed_column) for trigger in triggers]
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
                    dialect: self._renamed_raw_sql(expression, renamed_column)
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
            renamed_expression = self._renamed_raw_sql(field.expression, renamed_column)
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

    def _renamed_condition(self, condition: Any, renamed_column: tuple[str, str] | None) -> Any:
        """A constraint's or partial index's condition reading the renamed field - a ``Q`` by its
        field names, raw SQL by its column.

        Args:
            condition: The condition.
            renamed_column: The (old, new) DB column pair, or None when the column is unchanged.

        Returns:
            The rewritten condition, or None when it doesn't read the renamed field.
        """
        if isinstance(condition, Q):
            if self.old_name not in condition.get_referenced_field_names():
                return None
            return condition.with_renamed_field(self.old_name, self.new_name)
        renamed_sql = self._renamed_raw_sql(condition.sql, renamed_column)
        return RawSQLTerm(renamed_sql) if renamed_sql is not None else None

    def _renamed_trigger(self, trigger: Any, renamed_column: tuple[str, str] | None) -> Any:
        """Trigger.body/.when/.on are raw procedural SQL, not a structured field list - same
        word-boundary re.sub treatment `_renamed_constraint` gives CheckConstraint.check."""
        changes: dict[str, Any] = {}
        for attribute in ("body", "when", "on"):
            value = getattr(trigger, attribute)
            if value is not None and (renamed_value := self._renamed_raw_sql(value, renamed_column)) is not None:
                changes[attribute] = renamed_value
        return replace(trigger, **changes) if changes else trigger

    def _renamed_index(self, index: Index, renamed_column: tuple[str, str] | None) -> Index:
        """An index with the field renamed in its fields and, for a partial index, its condition - a
        copy.
        """
        fields_changed = self.old_name in index.fields or self.old_name in index.include
        new_condition = (
            self._renamed_condition(index.condition, renamed_column)
            if isinstance(index, PartialIndex) and index.condition is not None
            else None
        )
        condition_changed = new_condition is not None

        if not fields_changed and not condition_changed:
            return index

        if condition_changed:
            # Rebuilt from its constructor arguments rather than patched: a subclass (e.g. an
            # HNSW/IVFFlat index) folds its own WITH (...) parameters into `extra` ahead of the
            # WHERE clause, which only its own __init__ knows how to render.
            _path, args, kwargs = index.deconstruct()
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
                    renamed_sql = self._renamed_raw_sql(field_name.sql, renamed_column)
                    if renamed_sql is not None:
                        renamed_expressions.append((RawSQLTerm(renamed_sql), operator))
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

    async def _apply_rename_column(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None,
        old_field_name: str,
        new_field_name: str,
    ) -> None:
        if not state_editor:
            return
        old_model = self._model(old_state, app_label)
        new_model = self._model(new_state, app_label)
        old_field = old_model._meta.fields_map[old_field_name]
        new_field = new_model._meta.fields_map[new_field_name]
        if isinstance(old_field, ManyToManyFieldInstance):
            # A many-to-many field has no column on this table.
            return
        old_columns = self._get_db_column_names(old_field)
        new_columns = self._get_db_column_names(new_field)
        if old_columns == new_columns:
            return
        qualified_table = state_editor._qualify_table_name(new_model._meta.db_table, new_model._meta.schema)
        for old_column, new_column in zip(old_columns, new_columns, strict=True):
            if old_column == new_column:
                continue
            await state_editor._run_sql(
                state_editor.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table,
                    old_column=state_editor.quote(old_column),
                    new_column=state_editor.quote(new_column),
                )
            )
        await self._recreate_rewritten_triggers(old_model, new_model, state_editor)

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
            await state_editor.alter_trigger(new_model, old_trigger, new_trigger)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_rename_column(app_label, old_state, new_state, state_editor, self.old_name, self.new_name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_rename_column(app_label, old_state, new_state, state_editor, self.new_name, self.old_name)
