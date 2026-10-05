from __future__ import annotations

from dataclasses import replace
from typing import Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.fields.field import Field
from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.drift.observed.column_field_names import ColumnFieldNames
from hare.migrations.drift.observed.observed_indexes import ObservedIndexes
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption
from hare.sql.enums import Order


class ObservedUniqueness:
    """The unique indexes of a table matched to the declared unique constraints and unique indexes -
    the same fields and the same condition, written back the database's own way - and the fields
    made unique by them."""

    @staticmethod
    def get_declared_unique_entries(
        model_state: ModelState, table_name: str
    ) -> list[tuple[UniqueConstraint | Index, tuple[str, ...], str]]:
        """Lists the declared entries a unique index in the database can stand for.

        Args:
            model_state: The current model's state.
            table_name: The model's table.

        Returns:
            (UniqueConstraint or plain btree Index(unique=True), its column names, the name its
            index has in the database) triples.
        """
        column_names_by_field_name = ColumnFieldNames.field_to_column_names(model_state.fields)
        entries: list[tuple[UniqueConstraint | Index, tuple[str, ...], str]] = []
        for constraint in model_state.options.get(ModelOption.CONSTRAINTS, ()):
            if not isinstance(constraint, UniqueConstraint):
                continue
            column_names = tuple(
                column_name
                for field_name in constraint.fields
                for column_name in column_names_by_field_name.get(field_name, (field_name,))
            )
            entries.append(
                (
                    constraint,
                    column_names,
                    constraint.name
                    or GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_CONSTRAINT, table_name, column_names),
                )
            )
        for index in model_state.options.get(ModelOption.INDEXES, ()):
            if (
                type(index) not in {Index, PartialIndex}
                or not index.unique
                or not index.fields
                or index.opclasses
                or (isinstance(index, PartialIndex) and index.condition is not None)
            ):
                continue
            column_names = tuple(
                column_name
                for field_name in index.fields
                for column_name in column_names_by_field_name.get(field_name, (field_name,))
            )
            entries.append(
                (
                    index,
                    column_names,
                    index.name
                    or GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_INDEX, table_name, column_names),
                )
            )
        return entries

    @staticmethod
    def unique_index_fits_declared_entry(
        introspector_class: type[SchemaIntrospector], index: IndexInfo, declared: UniqueConstraint | Index
    ) -> bool:
        """Whether a unique index has the shape of a declared entry, its columns and name aside.

        Args:
            introspector_class: The database's introspector - it parses the predicate.
            index: The introspected unique index.
            declared: The declared UniqueConstraint or Index(unique=True).

        Returns:
            True for a plain unique index against an unconditional entry, or a partial one whose
            predicate (when it could be parsed) equals a conditional UniqueConstraint's.
        """
        if index.index_type or index.opclasses or index.expression_terms is not None:
            return False
        if isinstance(declared, UniqueConstraint) and any(
            key_order != Order.ASC_NULLS_LAST for key_order in index.key_orders
        ):
            return False
        declared_condition = declared.condition if isinstance(declared, UniqueConstraint) else None
        if declared_condition is None or index.condition_sql is None:
            return declared_condition is None and index.condition_sql is None
        return ObservedIndexes.condition_matches(introspector_class, declared_condition, index.condition_sql)

    @staticmethod
    def match_declared_unique_indexes(
        introspector_class: type[SchemaIntrospector], model_state: ModelState, table_info: TableInfo
    ) -> list[tuple[IndexInfo, UniqueConstraint | Index]]:
        """Pairs each unique index in the database with the declared UniqueConstraint or
        Index(unique=True) it implements - including a single-column one the introspector folds
        into ColumnInfo.is_unique, and a conditional UniqueConstraint's partial index.

        An index is paired by its name first; one left over is then paired by columns alone and
        reconstructed under its database name, so a renamed entry still shows up as a rename.

        Args:
            introspector_class: The database's introspector - it tells a name the database made up.
            model_state: The current model's state.
            table_info: The introspected table.

        Returns:
            (introspected index, the declared entry as the database has it) pairs.
        """
        declared_entries = ObservedUniqueness.get_declared_unique_entries(model_state, table_info.name)
        unique_indexes = [index for index in (*table_info.indexes, *table_info.column_indexes) if index.is_unique]
        column_to_field_name = ColumnFieldNames.column_to_field_name(model_state.fields)
        matches: list[tuple[IndexInfo, UniqueConstraint | Index]] = []
        matched_index_ids: set[int] = set()
        matched_entry_positions: set[int] = set()
        for match_by_name in (True, False):
            for position, (declared, column_names, index_name) in enumerate(declared_entries):
                if position in matched_entry_positions:
                    continue
                for index in unique_indexes:
                    if id(index) in matched_index_ids or tuple(index.columns) != column_names:
                        continue
                    if match_by_name and index.name != index_name:
                        continue
                    if not ObservedUniqueness.unique_index_fits_declared_entry(introspector_class, index, declared):
                        continue
                    database_name = (
                        None
                        if introspector_class.is_unnamed_index_name(index.name, table_info.name, index.columns)
                        else index.name
                    )
                    include = ObservedIndexes.get_observed_include(index, declared.include, column_to_field_name)
                    observed: UniqueConstraint | Index
                    if isinstance(declared, UniqueConstraint):
                        # NULLS DISTINCT is the database's default - a declaration stating it matches.
                        nulls_distinct = (
                            False if index.nulls_not_distinct else (True if declared.nulls_distinct else None)
                        )
                        observed = replace(
                            declared,
                            name=declared.name and (database_name or declared.name),
                            deferrable=index.deferrable,
                            initially_deferred=index.initially_deferred,
                            include=include,
                            nulls_distinct=nulls_distinct,
                        )
                    else:
                        observed = Index(
                            fields=tuple(
                                index.get_declared_fields(
                                    [column_to_field_name.get(column, column) for column in index.columns]
                                )
                            ),
                            name=database_name or declared.name,
                            unique=True,
                            include=include,
                        )
                    matches.append((index, observed))
                    matched_index_ids.add(id(index))
                    matched_entry_positions.add(position)
                    break
        return matches

    @staticmethod
    def apply_declared_uniqueness(
        model_state: ModelState, observed_fields: dict[str, Field[Any]], observed_options: dict[str, Any]
    ) -> None:
        """Gives the observed state the uniqueness the model declares, on a database without unique
        constraints (``Features.supports_unique_constraints``) - the schema editor creates none there,
        so the database can't differ from the declaration in it.

        Args:
            model_state: The model's current state.
            observed_fields: The fields as the database has them, changed in place.
            observed_options: The Meta options as the database has them, changed in place.
        """
        for field_name, observed_field in observed_fields.items():
            declared_field = model_state.fields.get(field_name)
            if declared_field is not None and observed_field is not declared_field:
                observed_field.unique = declared_field.unique
        declared_options = model_state.options
        declared_unique_constraints = [
            constraint
            for constraint in declared_options.get(ModelOption.CONSTRAINTS, ())
            if isinstance(constraint, UniqueConstraint)
        ]
        constraints = (
            *(
                constraint
                for constraint in observed_options.get(ModelOption.CONSTRAINTS, ())
                if not isinstance(constraint, UniqueConstraint)
            ),
            *declared_unique_constraints,
        )
        if constraints:
            observed_options[ModelOption.CONSTRAINTS] = constraints
        else:
            observed_options.pop(ModelOption.CONSTRAINTS, None)
        # A declared Index(unique=True) is a plain index of the same columns there.
        declared_unique_indexes = [
            index
            for index in declared_options.get(ModelOption.INDEXES, ())
            if isinstance(index, Index) and index.unique
        ]
        if declared_unique_indexes:
            observed_indexes = list(observed_options.get(ModelOption.INDEXES, ()))
            for declared_index in declared_unique_indexes:
                for position, observed_index in enumerate(observed_indexes):
                    if (
                        isinstance(observed_index, Index)
                        and not observed_index.unique
                        and tuple(observed_index.fields) == tuple(declared_index.fields)
                        and observed_index.expressions == declared_index.expressions
                    ):
                        observed_indexes[position] = declared_index
                        break
            observed_options[ModelOption.INDEXES] = tuple(observed_indexes)
