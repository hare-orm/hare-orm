from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.fields.field import Field
from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.drift.observed.column_field_names import ColumnFieldNames
from hare.migrations.drift.observed.observed_indexes import ObservedIndexes
from hare.migrations.drift.observed.observed_uniqueness import ObservedUniqueness
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption


class ObservedMetaOptions:
    """The Meta indexes, constraints and triggers of a model state replaced with the ones the database
    has - an exclusion constraint matched by its expressions, a trigger by its definition."""

    @staticmethod
    def build_observed_options(
        introspector_class: type[SchemaIntrospector],
        model_state: ModelState,
        table_info: TableInfo,
        observed_fields: dict[str, Field[Any]],
        matched_unique_indexes: list[tuple[IndexInfo, UniqueConstraint | Index]] | None = None,
        unique_is_plain: bool = False,
    ) -> dict[str, Any]:
        """Replaces the ``Meta`` indexes, constraints and triggers of ``model_state.options`` with what
        the database has - the database state must not share the declared options. A unique index
        matched to a declared ``UniqueConstraint`` or ``Index(unique=True)`` becomes that entry; any
        other multi-column unique index an unnamed ``UniqueConstraint``.
        """
        options = dict(model_state.options)
        column_to_field_name = ColumnFieldNames.column_to_field_name(model_state.fields)
        if matched_unique_indexes is None:
            matched_unique_indexes = ObservedUniqueness.match_declared_unique_indexes(
                introspector_class, model_state, table_info
            )
        matched_index_infos = [index for index, _declared in matched_unique_indexes]
        matched_index_ids = {id(index) for index in matched_index_infos}

        observed_indexes = ObservedIndexes.build_observed_indexes(
            introspector_class,
            table_info,
            column_to_field_name,
            model_state.options.get(ModelOption.INDEXES, ()),
            matched_index_infos,
            ObservedIndexes.get_relation_index_columns(model_state.fields),
        )
        observed_indexes.extend(declared for _index, declared in matched_unique_indexes if isinstance(declared, Index))
        # A plain single-field Meta.indexes entry is folded into its column's has_index flag -
        # an existing column index stands for the declared entry itself.
        indexed_column_names = {column.name for column in table_info.columns if column.has_index}
        column_names_by_field_name = ColumnFieldNames.field_to_column_names(model_state.fields)
        declared_single_field_indexes = ObservedIndexes.get_declared_single_field_indexes(model_state, unique_is_plain)
        for field_name, declared_entries in declared_single_field_indexes.items():
            field_column_names = column_names_by_field_name.get(field_name, (field_name,))
            if len(field_column_names) == 1 and field_column_names[0] in indexed_column_names:
                observed_indexes.extend(
                    entry if isinstance(entry, Index) else Index(fields=tuple(entry)) for entry in declared_entries
                )
        # A single-column db_index=True index is reported as ColumnInfo.has_index - it gets the same
        # synthetic Index the declared side has.
        observed_indexes.extend(ModelState.implicit_field_indexes(observed_fields, observed_indexes))
        declared_expression_indexes_by_name = {
            name: index
            for name, index in ObservedIndexes.get_declared_indexes_by_name(
                table_info.name, column_to_field_name, model_state.options.get(ModelOption.INDEXES, ())
            ).items()
            if index.expressions
        }
        for observed_index in observed_indexes:
            if observed_index.expressions and observed_index.name in declared_expression_indexes_by_name:
                # Postgres prints an expression index its own way - the declared expression of the
                # same name is kept, as for check and exclusion conditions below.
                declared_index = declared_expression_indexes_by_name[observed_index.name]
                observed_index.expressions = declared_index.expressions
                observed_index.declared_expressions = declared_index.declared_expressions
        if observed_indexes:
            options[ModelOption.INDEXES] = tuple(observed_indexes)
        else:
            options.pop(ModelOption.INDEXES, None)

        observed_unique_constraints = [
            declared for _index, declared in matched_unique_indexes if isinstance(declared, UniqueConstraint)
        ]
        for index in table_info.indexes:
            if not index.is_unique or index.is_special() or id(index) in matched_index_ids:
                continue
            observed_unique_constraints.append(
                UniqueConstraint(fields=tuple(column_to_field_name.get(column, column) for column in index.columns))
            )

        declared_exclusion_constraints = {
            constraint.name: constraint
            for constraint in model_state.options.get(ModelOption.CONSTRAINTS, ())
            if isinstance(constraint, ExclusionConstraint)
        }
        observed_constraints_list: list[ExclusionConstraint | CheckConstraint | UniqueConstraint] = list(
            observed_unique_constraints
        )
        for constraint in table_info.exclusion_constraints:
            expressions = tuple(
                (
                    column_to_field_name.get(expression, expression) if isinstance(expression, str) else expression,
                    operator,
                )
                for expression, operator in constraint.expressions
            )
            # Postgres prints a WHERE predicate its own way: when every other part matches the
            # declared constraint of the same name, its wording is kept. A real change still shows
            # in the expressions or method.
            declared = declared_exclusion_constraints.get(constraint.name)
            condition = constraint.condition
            if (
                declared is not None
                and declared.using == constraint.using
                and ObservedMetaOptions.exclusion_expressions_match(declared.expressions, expressions)
            ):
                # Raw SQL expressions come back rewritten the same way a WHERE predicate does.
                expressions = declared.expressions
            if declared is not None and declared.expressions == expressions and declared.using == constraint.using:
                condition = declared.condition
            observed_constraints_list.append(
                ExclusionConstraint(
                    name=constraint.name,
                    expressions=expressions,
                    using=constraint.using,
                    condition=condition,
                    include=tuple(column_to_field_name.get(column, column) for column in constraint.include),
                    deferrable=constraint.deferrable,
                    initially_deferred=constraint.initially_deferred,
                )
            )
        declared_check_constraints = {
            constraint.name: constraint
            for constraint in model_state.options.get(ModelOption.CONSTRAINTS, ())
            if isinstance(constraint, CheckConstraint)
        }
        for constraint in table_info.check_constraints:
            # The same for a CHECK predicate.
            declared_check = declared_check_constraints.get(constraint.name)
            check = declared_check.check if declared_check is not None else constraint.check
            observed_constraints_list.append(CheckConstraint(name=constraint.name, check=check))
        observed_constraints = tuple(observed_constraints_list)
        if observed_constraints:
            options[ModelOption.CONSTRAINTS] = observed_constraints
        else:
            options.pop(ModelOption.CONSTRAINTS, None)

        declared_triggers = {trigger.name: trigger for trigger in model_state.options.get(ModelOption.TRIGGERS, ())}
        observed_triggers = [
            ObservedMetaOptions.get_observed_trigger(trigger, declared_triggers.get(trigger.name))
            for trigger in table_info.triggers
        ]
        if observed_triggers:
            options[ModelOption.TRIGGERS] = tuple(observed_triggers)
        else:
            options.pop(ModelOption.TRIGGERS, None)

        return options

    @staticmethod
    def get_observed_trigger(observed: Trigger, declared: Trigger | None) -> Trigger:
        """The trigger to diff the declared one against: when the declared trigger of the same name has
        the same timing, level, events, language and deferral, its WHEN text and - if equal once
        stripped - its body replace what the database printed.

        Args:
            observed: The trigger as introspected.
            declared: The declared trigger of the same name, if any.

        Returns:
            The trigger.
        """
        if declared is None or (
            observed.timing,
            observed.for_each,
            observed.on,
            observed.language,
            observed.deferrable,
            observed.initially_deferred,
        ) != (
            declared.timing,
            declared.for_each,
            SchemaIntrospector.normalize_event_clause(declared.on),
            declared.language,
            declared.deferrable,
            declared.initially_deferred,
        ):
            return observed
        return replace(
            observed,
            on=declared.on,
            when=declared.when if observed.when is not None and declared.when is not None else observed.when,
            body=declared.body if observed.body.sql.strip() == declared.body.sql.strip() else observed.body,
        )

    @staticmethod
    def exclusion_expressions_match(
        declared_expressions: Iterable[tuple[Any, str]], observed_expressions: Iterable[tuple[Any, str]]
    ) -> bool:
        """Whether two ExclusionConstraint expression lists agree on everything but raw SQL text.

        Args:
            declared_expressions: The declared ``(expression, operator)`` pairs.
            observed_expressions: The introspected ``(expression, operator)`` pairs.

        Returns:
            True when both have the same length and operators, the same field names in the same
            positions, and a RawSQLTerm wherever the other side has one.
        """
        declared_pairs = list(declared_expressions)
        observed_pairs = list(observed_expressions)
        if len(declared_pairs) != len(observed_pairs):
            return False
        for (declared_expression, declared_operator), (observed_expression, observed_operator) in zip(
            declared_pairs, observed_pairs, strict=True
        ):
            if declared_operator != observed_operator:
                return False
            declared_is_raw = isinstance(declared_expression, RawSQLTerm)
            if declared_is_raw != isinstance(observed_expression, RawSQLTerm):
                return False
            if not declared_is_raw and declared_expression != observed_expression:
                return False
        return True
