from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption
from hare.query.expressions import Q


class ObservedIndexes:
    """The indexes a table has, read back against the declared ones: an index named the same and
    matching its fields, key order and included columns, and the indexes of relations hare creates
    by itself."""

    @staticmethod
    def get_index_field_names(column_names: Iterable[str], column_to_field_name: dict[str, str]) -> tuple[str, ...]:
        """Maps an index's columns to field names, naming a relation once for its run of key columns.

        Args:
            column_names: The index's columns, in order.
            column_to_field_name: Real column name -> owning field name.

        Returns:
            The field names, in order.
        """
        field_names: list[str] = []
        for column_name in column_names:
            field_name = column_to_field_name.get(column_name, column_name)
            if field_names and field_name != column_name and field_names[-1] == field_name:
                continue
            field_names.append(field_name)
        return tuple(field_names)

    @staticmethod
    def get_declared_single_field_indexes(
        model_state: ModelState, unique_is_plain: bool = False
    ) -> dict[str, list[Any]]:
        """Groups the model's plain single-field Meta.indexes entries by field name.

        The introspector folds such an index into ColumnInfo.has_index, so it never shows up as
        its own IndexInfo.

        Args:
            model_state: The current model's state.
            unique_is_plain: Whether a unique entry is a plain index in the database - one without
                unique constraints (``Features.supports_unique_constraints``).

        Returns:
            Field name -> the declared entries (Index objects or field-name tuples) covering it.
        """
        entries_by_field_name: dict[str, list[Any]] = {}
        for entry in model_state.options.get(ModelOption.INDEXES, ()):
            index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if type(index) is not Index or index.expressions or index.opclasses:
                continue
            if index.unique and not unique_is_plain:
                continue
            if len(index.fields) != 1:
                continue
            entries_by_field_name.setdefault(index.fields[0], []).append(entry)
        return entries_by_field_name

    @staticmethod
    def has_relation_index(field: ForeignKeyFieldInstance[Any], table_info: TableInfo) -> bool:
        """Whether the database has the index a foreign key's ``db_index=True`` implies.

        Args:
            field: The foreign key field.
            table_info: The introspected table.

        Returns:
            True when its one key column has a plain index, or a plain index spans exactly all of
            its key columns.
        """
        column_names = tuple(field.db_column_names)
        if not column_names:
            # Relations not initialized yet name no key columns to look for.
            return field.index
        if len(column_names) == 1:
            return any(column.name == column_names[0] and column.has_index for column in table_info.columns)
        return any(
            tuple(index.columns) == column_names
            for index in table_info.indexes
            if not index.is_unique and not index.is_special()
        )

    @staticmethod
    def get_relation_index_columns(fields: dict[str, Field[Any]]) -> set[tuple[str, ...]]:
        """The key columns of every indexed foreign key to a composite primary key - its index is
        compared as the field's own index flag, not as a Meta.indexes entry.

        Args:
            fields: The model's declared fields.

        Returns:
            Each such foreign key's key columns, in order.
        """
        return {
            tuple(field.db_column_names)
            for field in fields.values()
            if isinstance(field, ForeignKeyFieldInstance) and field.index and len(field.db_column_names) > 1
        }

    @staticmethod
    def is_same_key_order(declared: Index, observed: Index, column_names: list[str]) -> bool:
        """Whether a declared index spelling its keys as orderings is the observed index over plain
        columns - the same columns in the same direction and NULL placement.

        Args:
            declared: The declared index of the same name.
            observed: The index as the database has it.
            column_names: The observed index's key columns.

        Returns:
            True when they are one index.
        """
        if declared.fields or type(declared) is not type(observed) or declared.include != observed.include:
            return False
        declared_columns = [
            getattr(key.term if isinstance(key, OrderedIndexKey) else key, "name", None)
            for key in declared.expressions
        ]
        return declared_columns == list(column_names) and declared.get_key_orders() == observed.get_key_orders()

    @staticmethod
    def get_observed_include(
        index: IndexInfo, declared_include: Iterable[str], column_to_field_name: dict[str, str]
    ) -> tuple[str, ...]:
        """The non-key fields an index has - the declared ones where the database keeps none (SQLite).

        Args:
            index: The introspected index.
            declared_include: The declared entry's ``include``.
            column_to_field_name: Real column name -> owning field name.

        Returns:
            The field names.
        """
        if index.include is None:
            return tuple(declared_include)
        return tuple(column_to_field_name.get(column, column) for column in index.include)

    @staticmethod
    def get_declared_indexes_by_name(
        table_name: str, column_to_field_name: dict[str, str], declared_indexes: Iterable[Any]
    ) -> dict[str, Index]:
        """Keys each declared Index entry by the name it has in the database.

        Args:
            table_name: The model's table.
            column_to_field_name: Real column name -> owning field name.
            declared_indexes: The model's declared Meta.indexes entries.

        Returns:
            Index name (explicit, or the one generated from table and columns) -> declared index.
        """
        first_column_by_field_name: dict[str, str] = {}
        for column_name, field_name in column_to_field_name.items():
            first_column_by_field_name.setdefault(field_name, column_name)
        indexes_by_name: dict[str, Index] = {}
        for index in declared_indexes:
            if not isinstance(index, Index):
                continue
            if index.name:
                indexes_by_name[index.name] = index
            elif index.fields and not index.expressions:
                column_names = [first_column_by_field_name.get(name, name) for name in index.fields]
                generated_name = GeneratedNames.get_index_name(
                    GeneratedNamePrefix.UNIQUE_INDEX if index.unique else GeneratedNamePrefix.INDEX,
                    table_name,
                    column_names,
                    index.get_name_parts(),
                )
                indexes_by_name[generated_name] = index
            elif index.expressions:
                # An expression index is named after its rendered terms, available only once
                # they're resolved - as they are in migration and drift state.
                try:
                    expression_terms = index.field_names
                except ConfigurationError:
                    continue
                generated_name = GeneratedNames.get_index_name(
                    GeneratedNamePrefix.UNIQUE_INDEX if index.unique else GeneratedNamePrefix.INDEX,
                    table_name,
                    expression_terms,
                    index.get_name_parts(),
                )
                indexes_by_name[generated_name] = index
        return indexes_by_name

    @staticmethod
    def build_observed_indexes(
        introspector_class: type[SchemaIntrospector],
        table_info: TableInfo,
        column_to_field_name: dict[str, str],
        declared_indexes: Iterable[Any] = (),
        matched_unique_indexes: Iterable[IndexInfo] = (),
        relation_index_columns: set[tuple[str, ...]] | frozenset[tuple[str, ...]] = frozenset(),
    ) -> list[Index]:
        """Reconstructs the Meta.indexes entries the database has.

        Args:
            introspector_class: The database's introspector - it names the dialect's own index
                classes.
            table_info: The introspected table.
            column_to_field_name: Real column name -> owning field name.
            declared_indexes: The model's declared Meta.indexes entries.
            matched_unique_indexes: Unique indexes already reconstructed as a declared
                UniqueConstraint or Index(unique=True) - left out here.
            relation_index_columns: Key columns whose plain index is an indexed foreign key's
                own - left out here.

        Returns:
            The observed indexes.
        """
        declared_indexes_by_name = ObservedIndexes.get_declared_indexes_by_name(
            table_info.name, column_to_field_name, declared_indexes
        )
        matched_unique_index_ids = {id(index) for index in matched_unique_indexes}
        indexes: list[Index] = []
        for index in table_info.indexes:
            if index.is_special() or index.is_unique:
                continue
            if tuple(index.columns) in relation_index_columns:
                continue
            fields = tuple(
                index.get_declared_fields(
                    list(ObservedIndexes.get_index_field_names(index.columns, column_to_field_name))
                )
            )
            declared_index = declared_indexes_by_name.get(index.name)
            declared_include = declared_index.include if declared_index is not None else ()
            include = ObservedIndexes.get_observed_include(index, declared_include, column_to_field_name)
            observed_index = Index(fields=fields, name=index.name or None, include=include)
            if declared_index is not None and ObservedIndexes.is_same_key_order(
                declared_index, observed_index, index.columns
            ):
                # F("a").desc(nulls_first=True) is the database's default placement - stored and
                # read back as a plain "-a" key.
                observed_index = declared_index
            indexes.append(observed_index)

        for index in table_info.indexes:
            if not index.is_special() or id(index) in matched_unique_index_ids:
                continue
            index_class, index_args, index_kwargs = index.get_index_declaration(
                introspector_class.INDEX_CLASSES_BY_TYPE, column_to_field_name, as_key_terms=True
            )
            declared_index = declared_indexes_by_name.get(index.name)
            if declared_index is not None and tuple(declared_index.get_declared_fields()) != tuple(
                index_kwargs.get("fields") or ()
            ):
                declared_index = None
            if declared_index is not None:
                # What the database doesn't report, or reports reworded, is taken as declared.
                if index.include is None and declared_index.include:
                    index_kwargs["include"] = list(declared_index.include)
                # No opclasses are reported when every one is the access method's default - a
                # declaration naming exactly those defaults is the same index.
                if (
                    not index.opclasses
                    and index.default_opclasses
                    and tuple(declared_index.opclasses) == tuple(index.default_opclasses)
                ):
                    index_kwargs["opclasses"] = tuple(declared_index.opclasses)
                if (
                    index.condition_sql is not None
                    and isinstance(declared_index, PartialIndex)
                    and declared_index.condition is not None
                    and ObservedIndexes.condition_matches(
                        introspector_class, declared_index.condition, index.condition_sql
                    )
                ):
                    index_kwargs["condition"] = declared_index.condition
            if index.name:
                index_kwargs["name"] = index.name
            indexes.append(index_class(*index_args, **index_kwargs))
        return indexes

    @staticmethod
    def condition_matches(
        introspector_class: type[SchemaIntrospector], declared_condition: Q | RawSQLTerm, condition_sql: str
    ) -> bool:
        """Whether a partial index's predicate in the database is the declared condition. The
        database writes a predicate back its own way (quoted identifiers, type casts, extra
        parentheses), so the declared wording wins unless both are equalities of literals that
        differ; a ``Q`` has no SQL text of its own and is trusted.

        Args:
            introspector_class: The database's introspector - it parses both predicates.
            declared_condition: The declared condition.
            condition_sql: The predicate the database reports.

        Returns:
            True unless the two differ.
        """
        if isinstance(declared_condition, Q):
            return True
        declared_equalities = introspector_class.get_predicate_equalities(declared_condition.sql)
        observed_equalities = introspector_class.get_predicate_equalities(condition_sql)
        return declared_equalities is None or observed_equalities is None or declared_equalities == observed_equalities
