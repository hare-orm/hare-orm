from collections.abc import Iterable
from copy import copy
from dataclasses import replace
from typing import Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.indexes.partial_index import PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.table_options import TableOptions
from hare.ddl.triggers import Trigger
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.fields.base.field import Field
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.swappable import SwappableModelReference
from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.types.column_info import ColumnInfo
from hare.inspectdb.types.index_info import IndexInfo
from hare.inspectdb.types.table_info import TableInfo
from hare.migrations.constants import (
    RELATION_FIELDS,
)
from hare.migrations.drift.column_definition_comparer import ColumnDefinitionComparer
from hare.migrations.drift.column_mismatch import ColumnMismatch
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.state.project.state import State
from hare.models.enums import ModelOption
from hare.query.expressions import Q
from hare.sql.enums import Order
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME


class DriftStateBuilder:
    """Builds the "as the database actually is" State that `detect_drift()` diffs the current
    models against - split out from that one call so each step (expected columns, per-table
    introspection, observed-field overrides) stays independently readable/testable."""

    @staticmethod
    def _expected_columns(fields: dict[str, Field[Any]]) -> set[str]:
        """Every column ``fields`` own - every key column of a relation, source_field or name of a
        plain field, none for a many-to-many field.
        """
        columns: set[str] = set()
        for name, field in fields.items():
            if isinstance(field, ManyToManyFieldInstance):
                continue
            if isinstance(field, RelationalField):
                columns.update(field.db_column_names)
                continue
            columns.add(field.source_field or name)
        return columns

    @staticmethod
    def _auto_managed_through_tables(fields: dict[str, Field[Any]]) -> set[str]:
        """Names of the through tables hare itself creates for `fields`' many-to-many relations - a
        user-declared ``through=SomeModel`` table is that model's own table, tracked as such."""
        return {
            field.through
            for field in fields.values()
            if isinstance(field, ManyToManyFieldInstance) and field.through_model is None and field.through
        }

    @staticmethod
    def _get_index_field_names(column_names: Iterable[str], column_to_field_name: dict[str, str]) -> tuple[str, ...]:
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
    def _column_to_field_name(fields: dict[str, Field[Any]]) -> dict[str, str]:
        """Each column name to the field owning it (``event_id`` -> ``event``)."""
        mapping: dict[str, str] = {}
        for name, field in fields.items():
            if isinstance(field, ManyToManyFieldInstance):
                continue
            if isinstance(field, RelationalField):
                for db_column_name in field.db_column_names:
                    mapping[db_column_name] = name
                continue
            mapping[field.source_field or name] = name
        return mapping

    @staticmethod
    async def build_database_state(
        connection: DatabaseClient,
        new_state: State,
        target_labels: list[str],
        schema: str | None,
        unmanaged_model_states: "list[ModelState] | None" = None,
    ) -> tuple[State, list[str], list[tuple[str, str, str, str]], list[ColumnMismatch]]:
        """Builds the state of ``target_labels``'s models as the database has them: each field copied
        with the database's null/unique/index flags, column type, foreign key constraint and
        db_default. Types and defaults are compared normalized, and what can't be normalized counts
        as equal, so a schema made by ``migrate`` shows no drift. A type difference no field
        describes is a ColumnMismatch. A model whose table doesn't exist is left out - the diff
        reports its CreateModel. A model without Meta.schema is looked up in the connection's
        default schema.

        Args:
            connection: The connection whose database is read.
            new_state: The state the models declare.
            target_labels: The apps whose models are compared.
            schema: The schema swept for untracked tables - the default schema when None. Ignored on
                SQLite.
            unmanaged_model_states: The ``Meta.managed = False`` models' states - their tables count
                as known and aren't diffed.

        Returns:
            The database's state, the untracked tables, the untracked columns and the column
            mismatches.
        """
        dialect = connection.dialect.name
        introspector_class = SchemaIntrospector.get_introspector_class(connection)
        default_schema = await SchemaIntrospector.get_default_schema(connection)
        if schema is None or not connection.dialect.supports_schemas:
            schema = default_schema
        real_table_names = set(await SchemaIntrospector.get_table_names(connection, schema=schema))
        # A model is looked up in its own schema, not the swept one; each schema's tables are listed
        # once.
        real_table_names_by_other_schema: dict[str, set[str]] = {}
        db_state = State(models={}, apps=StateApps())
        # hare-orm's own bookkeeping tables - the migration log (on every migrated database) and
        # Transactions.distributed()'s decision log - are real tables no configured model backs,
        # so they would otherwise show up as "untracked" on every clean, fully in-sync database.
        known_tables: set[str] = {NoopRecorder().table_name, DISTRIBUTED_DECISIONS_TABLE_NAME}
        for unmanaged_model_state in unmanaged_model_states or ():
            if (unmanaged_model_state.options.get(ModelOption.SCHEMA) or default_schema) == schema:
                known_tables.add(unmanaged_model_state.table)
        untracked_columns: list[tuple[str, str, str, str]] = []
        mismatched_columns: list[ColumnMismatch] = []
        # The models to inspect, found against the listed tables - inspected together below, with a
        # fixed number of catalog queries per schema.
        to_inspect: list[tuple[tuple[str, str], ModelState, str, set[str]]] = []
        for key, model_state in new_state.models.items():
            app_label, _model_name = key
            if app_label not in target_labels:
                continue
            model_schema = model_state.options.get(ModelOption.SCHEMA) or default_schema
            if model_schema == schema:
                model_real_table_names = real_table_names
                known_tables.add(model_state.table)
                known_tables.update(DriftStateBuilder._auto_managed_through_tables(model_state.fields))
            else:
                # Not folded into `known_tables`/`untracked_tables` - those report what's extra
                # in `schema` specifically, a different schema's own tables are simply out of
                # scope for that sweep, not evidence of anything untracked in `schema`.
                if model_schema not in real_table_names_by_other_schema:
                    real_table_names_by_other_schema[model_schema] = set(
                        await SchemaIntrospector.get_table_names(connection, schema=model_schema)
                    )
                model_real_table_names = real_table_names_by_other_schema[model_schema]
            if model_state.table not in model_real_table_names:
                continue
            to_inspect.append((key, model_state, model_schema, model_real_table_names))

        # One batched inspect_tables() call per schema. verify_exists=False - every entry here was
        # already matched against a just-fetched table list above.
        to_inspect_positions_by_schema: dict[str, list[int]] = {}
        for position, (_key, _model_state, model_schema, _model_real_table_names) in enumerate(to_inspect):
            to_inspect_positions_by_schema.setdefault(model_schema, []).append(position)
        table_info_by_position: dict[int, TableInfo] = {}
        for model_schema, positions in to_inspect_positions_by_schema.items():
            schema_table_infos = await SchemaIntrospector.inspect_tables(
                connection,
                [to_inspect[position][1].table for position in positions],
                schema=model_schema,
                verify_exists=False,
            )
            table_info_by_position.update(zip(positions, schema_table_infos, strict=True))
        for position, (key, model_state, model_schema, model_real_table_names) in enumerate(to_inspect):
            table_info = table_info_by_position[position]
            app_label, model_name = key
            expected_columns = DriftStateBuilder._expected_columns(model_state.fields)
            for column in table_info.columns:
                if column.name not in expected_columns:
                    untracked_columns.append((app_label, model_name, model_state.table, column.name))
            (
                out_of_sync_many_to_many_field_names,
                unindexed_many_to_many_field_names,
            ) = await DriftStateBuilder._get_out_of_sync_many_to_many_field_names(
                connection, model_state.fields, model_real_table_names, model_schema
            )
            matched_unique_indexes = DriftStateBuilder._match_declared_unique_indexes(
                introspector_class, model_state, table_info
            )
            observed_fields = DriftStateBuilder._build_observed_fields(
                model_state.fields,
                table_info,
                out_of_sync_many_to_many_field_names,
                frozenset(
                    DriftStateBuilder._get_declared_single_field_indexes(
                        model_state, unique_is_plain=not connection.dialect.supports_unique_constraints
                    )
                ),
                [index for index, _declared in matched_unique_indexes],
                unindexed_many_to_many_field_names,
                dialect,
                default_schema,
            )
            mismatched_columns.extend(
                ColumnMismatch(app_label, model_name, model_state.table, column_name, detail)
                for column_name, detail in [
                    *DriftStateBuilder._get_column_mismatches(model_state.fields, table_info, dialect),
                    *DriftStateBuilder.get_swappable_foreign_key_mismatches(model_state.fields, table_info),
                ]
            )
            observed_options = DriftStateBuilder._build_observed_options(
                introspector_class,
                model_state,
                table_info,
                observed_fields,
                matched_unique_indexes,
                unique_is_plain=not connection.dialect.supports_unique_constraints,
            )
            if not connection.dialect.supports_unique_constraints:
                DriftStateBuilder._apply_declared_uniqueness(model_state, observed_fields, observed_options)
            DriftStateBuilder._apply_observed_table_options(
                introspector_class, dialect, model_state, table_info, observed_options
            )
            db_state.models[key] = ModelState(
                name=model_state.name,
                app=model_state.app,
                table=model_state.table,
                abstract=model_state.abstract,
                description=model_state.description,
                options=observed_options,
                bases=model_state.bases,
                pk_field_name=model_state.pk_field_name,
                fields=observed_fields,
            )
        untracked_tables = sorted(real_table_names - known_tables)
        return db_state, untracked_tables, sorted(untracked_columns), mismatched_columns

    @staticmethod
    async def _get_out_of_sync_many_to_many_field_names(
        connection: DatabaseClient, model_fields: dict[str, Field[Any]], real_table_names: set[str], schema: str
    ) -> tuple[set[str], set[str]]:
        """Finds the many-to-many fields whose hare-managed through table is missing or broken.

        Args:
            connection: The database connection to introspect.
            model_fields: The model's declared fields.
            real_table_names: Tables that exist in `schema`.
            schema: The schema the model's tables live in.

        Returns:
            Names of the many-to-many fields whose through table is missing, lacks one of its key
            columns, or lacks the UNIQUE constraint over them the field declares, and names of the
            ones whose through table lacks a key index their ``db_index=True`` declares.
        """
        out_of_sync_field_names: set[str] = set()
        unindexed_field_names: set[str] = set()
        for field_name, field in model_fields.items():
            if not isinstance(field, ManyToManyFieldInstance) or field.through_model is not None or not field.through:
                continue
            if field.through not in real_table_names:
                out_of_sync_field_names.add(field_name)
                continue
            key_columns = {*field.backward_keys, *field.forward_keys}
            if not key_columns:
                continue
            through_table_info = await SchemaIntrospector.inspect_table(
                connection, field.through, schema=schema, verify_exists=False
            )
            if not key_columns <= {column.name for column in through_table_info.columns}:
                out_of_sync_field_names.add(field_name)
                continue
            if (
                field.unique
                and connection.dialect.supports_unique_constraints
                and not any(
                    index.is_unique and set(index.columns) == key_columns for index in through_table_info.indexes
                )
            ):
                out_of_sync_field_names.add(field_name)
                continue
            indexed_column_names = {column.name for column in through_table_info.columns if column.has_index}
            plain_index_columns = {
                tuple(index.columns)
                for index in through_table_info.indexes
                if not index.is_unique and not index.is_special()
            }
            if not all(
                index_keys in plain_index_columns or (len(index_keys) == 1 and index_keys[0] in indexed_column_names)
                for index_keys in field.get_through_index_keys()
            ):
                unindexed_field_names.add(field_name)
        return out_of_sync_field_names, unindexed_field_names

    @staticmethod
    def _field_to_column_names(fields: dict[str, Field[Any]]) -> dict[str, tuple[str, ...]]:
        """Maps each field name to the real database column(s) it owns - the inverse of
        _column_to_field_name, skipping a many-to-many (it owns no column on this table)."""
        column_names_by_field_name: dict[str, tuple[str, ...]] = {}
        for name, field in fields.items():
            if isinstance(field, ManyToManyFieldInstance):
                continue
            if isinstance(field, RelationalField):
                column_names_by_field_name[name] = tuple(field.db_column_names)
                continue
            column_names_by_field_name[name] = (field.source_field or name,)
        return column_names_by_field_name

    @staticmethod
    def _get_declared_single_field_indexes(
        model_state: ModelState, unique_is_plain: bool = False
    ) -> dict[str, list[Any]]:
        """Groups the model's plain single-field Meta.indexes entries by field name.

        The introspector folds such an index into ColumnInfo.has_index, so it never shows up as
        its own IndexInfo.

        Args:
            model_state: The current model's state.
            unique_is_plain: Whether a unique entry is a plain index in the database - one without
                unique constraints (``Dialect.supports_unique_constraints``).

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
    def _get_declared_unique_entries(
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
        column_names_by_field_name = DriftStateBuilder._field_to_column_names(model_state.fields)
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
                type(index) not in (Index, PartialIndex)
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
    def _unique_index_fits_declared_entry(index: IndexInfo, declared: UniqueConstraint | Index) -> bool:
        """Whether a unique index has the shape of a declared entry, its columns and name aside.

        Args:
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
        return DriftStateBuilder._condition_matches(declared_condition, index.condition_sql)

    @staticmethod
    def _condition_matches(declared_condition: Q | RawSQLTerm, condition_sql: str) -> bool:
        """Whether a partial index's predicate in the database is the declared condition. The
        database writes a predicate back its own way (quoted identifiers, type casts, extra
        parentheses), so the declared wording wins unless both are equalities of literals that
        differ; a ``Q`` has no SQL text of its own and is trusted.

        Args:
            declared_condition: The declared condition.
            condition_sql: The predicate the database reports.

        Returns:
            True unless the two differ.
        """
        if isinstance(declared_condition, Q):
            return True
        declared_equalities = SchemaIntrospector.get_predicate_equalities(declared_condition.sql)
        observed_equalities = SchemaIntrospector.get_predicate_equalities(condition_sql)
        return declared_equalities is None or observed_equalities is None or declared_equalities == observed_equalities

    @staticmethod
    def _match_declared_unique_indexes(
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
        declared_entries = DriftStateBuilder._get_declared_unique_entries(model_state, table_info.name)
        unique_indexes = [index for index in (*table_info.indexes, *table_info.column_indexes) if index.is_unique]
        column_to_field_name = DriftStateBuilder._column_to_field_name(model_state.fields)
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
                    if not DriftStateBuilder._unique_index_fits_declared_entry(index, declared):
                        continue
                    database_name = (
                        None
                        if introspector_class.is_unnamed_index_name(index.name, table_info.name, index.columns)
                        else index.name
                    )
                    include = DriftStateBuilder._get_observed_include(index, declared.include, column_to_field_name)
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
    def _build_observed_fields(
        model_fields: dict[str, Field[Any]],
        table_info: TableInfo,
        out_of_sync_many_to_many_field_names: set[str] | frozenset[str] = frozenset(),
        declared_single_field_index_names: frozenset[str] = frozenset(),
        matched_unique_indexes: Iterable[IndexInfo] = (),
        unindexed_many_to_many_field_names: set[str] | frozenset[str] = frozenset(),
        dialect: str | None = None,
        default_schema: str | None = None,
    ) -> dict[str, Field[Any]]:
        """Copies each field with what the database has: a plain field's null/unique/index flags,
        a relation's nullability, index and FOREIGN KEY constraint (target, ON DELETE), every
        declared db_default and, when the dialect is given, a column type other than the declared
        one.

        Args:
            model_fields: The current model's fields.
            table_info: The introspected table.
            out_of_sync_many_to_many_field_names: Many-to-many fields left out as missing.
            declared_single_field_index_names: Fields whose column index is a Meta.indexes entry.
            matched_unique_indexes: Unique indexes that implement a declared UniqueConstraint or
                Index(unique=True) - they don't make their column's field unique=True.
            unindexed_many_to_many_field_names: Many-to-many fields whose through table lacks a
                key index they declare.
            dialect: The database dialect - column types are compared only when given.
            default_schema: The connection's default schema, where a relation's target table
                without Meta.schema lives.

        Returns:
            Field name -> the field as the database has it.
        """
        columns_by_name = {column.name: column for column in table_info.columns}
        matched_unique_index_ids = {id(index) for index in matched_unique_indexes}
        unique_column_indexes_by_column_name: dict[str, list[IndexInfo]] = {}
        for column_index in table_info.column_indexes:
            if column_index.is_unique:
                unique_column_indexes_by_column_name.setdefault(column_index.columns[0], []).append(column_index)
        observed: dict[str, Field[Any]] = {}
        for field_name, field in model_fields.items():
            if field_name in out_of_sync_many_to_many_field_names:
                # Left out, like a missing column below - the diff reports the relation (and its
                # through table) as missing.
                continue
            if field_name in unindexed_many_to_many_field_names:
                observed_field = copy(field)
                observed_field.index = False
                observed[field_name] = observed_field
                continue
            if isinstance(field, ForeignKeyFieldInstance):
                observed[field_name] = DriftStateBuilder._get_observed_relation_field(
                    field, table_info, columns_by_name, dialect, default_schema
                )
                continue
            # Other relations own no column of this table, and a primary key column's own
            # backing constraint never counts as a separate unique/index entry - both are
            # trusted as-is.
            if field.pk or isinstance(field, RELATION_FIELDS):
                observed[field_name] = field
                continue
            column = columns_by_name.get(field.source_field or field_name)
            if column is None:
                # No matching column in the database - left out entirely, so the diff reports a
                # missing AddField instead of silently ignoring it.
                continue
            observed_field = copy(field)
            if dialect is not None:
                observed_field = (
                    DriftStateBuilder._get_field_of_observed_type(dialect, field, column) or observed_field
                )
            observed_field.null = column.nullable
            unique_column_indexes = unique_column_indexes_by_column_name.get(column.name)
            if unique_column_indexes:
                observed_field.unique = any(
                    id(unique_column_index) not in matched_unique_index_ids
                    for unique_column_index in unique_column_indexes
                )
            else:
                observed_field.unique = column.is_unique
            # A UNIQUE constraint's own backing index is not a separate db_index=True index -
            # has_index only reports a plain, non-unique one. A column index declared through a
            # Meta.indexes entry is compared as that entry instead of as the field's own flag.
            if column.has_index and field_name in declared_single_field_index_names:
                observed_field.index = field.index
            else:
                observed_field.index = column.has_index
            if dialect is not None:
                DriftStateBuilder._apply_observed_db_default(observed_field, field, column, dialect)
            observed[field_name] = observed_field
        return observed

    @staticmethod
    def _get_field_of_observed_type(dialect: str, field: Field[Any], column: ColumnInfo) -> Field[Any] | None:
        """A copy of a plain field with the column's type, when it differs from the declared one.

        Args:
            dialect: The database dialect.
            field: The declared field.
            column: The introspected column.

        Returns:
            The field resized to the column (a CharField/DecimalField whose size is all that
            differs) or the field inspectdb reconstructs the column as; None when the types agree
            or the difference can't be described by a field.
        """
        declared_type = ColumnDefinitionComparer.get_declared_column_type(field, dialect)
        if declared_type is None or not ColumnDefinitionComparer.column_types_differ(dialect, declared_type, column):
            return None
        return ColumnDefinitionComparer.get_resized_field(
            field, column
        ) or ColumnDefinitionComparer.get_field_of_column_type(dialect, field, column)

    @staticmethod
    def _apply_observed_db_default(
        observed_field: Field[Any], field: Field[Any], column: ColumnInfo, dialect: str
    ) -> None:
        """Sets the column's DEFAULT on the observed field when it isn't the declared db_default.

        Args:
            observed_field: The field as the database has it, updated in place.
            field: The declared field - only one with a db_default is compared.
            column: The introspected column.
            dialect: The database dialect.
        """
        if not field.has_db_default() or not ColumnDefinitionComparer.db_defaults_differ(field, column, dialect):
            return
        observed_field.db_default = DB_DEFAULT_NOT_SET if column.db_default is None else column.db_default

    @staticmethod
    def _get_observed_relation_field(
        field: ForeignKeyFieldInstance[Any],
        table_info: TableInfo,
        columns_by_name: dict[str, ColumnInfo],
        dialect: str | None,
        default_schema: str | None,
    ) -> ForeignKeyFieldInstance[Any]:
        """Copies a foreign key with its index, nullability, FOREIGN KEY constraint and db_default
        as the database has them.

        Args:
            field: The declared foreign key or one-to-one field.
            table_info: The introspected table.
            columns_by_name: The table's columns by name.
            dialect: The database dialect - the constraint and default are compared only when given,
                the constraint only where the database has foreign keys.
            default_schema: The connection's default schema.

        Returns:
            The field as the database has it - the declared one itself when its key columns
            aren't all there or its target isn't resolved yet.
        """
        observed_field = copy(field)
        if field.index and not field.pk:
            observed_field.index = DriftStateBuilder._has_relation_index(field, table_info)
        key_columns = [columns_by_name.get(column_name) for column_name in field.db_column_names]
        if not key_columns or None in key_columns or dialect is None:
            return observed_field
        present_key_columns = [column for column in key_columns if column is not None]
        if not field.pk:
            # A composite relation declared nullable makes every key column nullable.
            nullable_flags = [column.nullable for column in present_key_columns]
            observed_field.null = all(nullable_flags) if field.null else any(nullable_flags)
        if not DialectRegistry.get_dialect(dialect).supports_foreign_keys:
            # The database has no FOREIGN KEY constraints - db_constraint and on_delete describe
            # what hare enforces itself, nothing the database could differ in.
            if len(present_key_columns) == 1:
                DriftStateBuilder._apply_observed_db_default(observed_field, field, present_key_columns[0], dialect)
            return observed_field
        foreign_key_on_delete = DriftStateBuilder._get_observed_foreign_key_on_delete(
            field, table_info, default_schema
        )
        if field.db_constraint and foreign_key_on_delete is None:
            observed_field.db_constraint = False
        elif not field.db_constraint and foreign_key_on_delete is not None:
            observed_field.db_constraint = True
        elif (
            field.db_constraint
            and foreign_key_on_delete is not None
            and field.db_on_delete != foreign_key_on_delete
            and not field.db_on_delete.startswith(f"{foreign_key_on_delete} ")
        ):
            observed_field.on_delete = OnDelete(foreign_key_on_delete)
        if len(present_key_columns) == 1:
            DriftStateBuilder._apply_observed_db_default(observed_field, field, present_key_columns[0], dialect)
        return observed_field

    @staticmethod
    def _get_observed_foreign_key_on_delete(
        field: ForeignKeyFieldInstance[Any], table_info: TableInfo, default_schema: str | None
    ) -> str | None:
        """The ON DELETE action of the FOREIGN KEY constraint implementing a relation.

        Args:
            field: The declared foreign key or one-to-one field.
            table_info: The introspected table.
            default_schema: The connection's default schema.

        Returns:
            The action as the database spells it (``CASCADE``, ``NO ACTION``, ...); None when its
            key columns have no FOREIGN KEY constraint to the relation's target table.
        """
        column_names = tuple(field.db_column_names)
        related_meta = getattr(getattr(field, "related_model", None), "_meta", None)
        if len(column_names) == 1:
            foreign_key = table_info.foreign_keys.get(column_names[0])
            if foreign_key is None:
                return None
            if related_meta is not None:
                target_schema = related_meta.schema or default_schema
                if foreign_key.target_table != related_meta.db_table or (
                    foreign_key.target_schema is not None
                    and target_schema is not None
                    and foreign_key.target_schema != target_schema
                ):
                    return None
            return str(foreign_key.on_delete)
        for composite_foreign_key in table_info.composite_foreign_keys:
            if set(composite_foreign_key.columns) == set(column_names):
                if related_meta is not None and composite_foreign_key.target_table != related_meta.db_table:
                    return None
                return str(composite_foreign_key.on_delete)
        for _name, member_columns in table_info.unparsed_foreign_keys:
            if set(member_columns) == set(column_names):
                # A constraint that couldn't be read back in full is trusted to be the declared one.
                return field.db_on_delete
        return None

    @staticmethod
    def get_swappable_foreign_key_mismatches(
        model_fields: dict[str, Field[Any]], table_info: TableInfo
    ) -> list[tuple[str, str]]:
        """Lists the foreign keys declared with ``swappable()`` whose FOREIGN KEY constraint
        references another table than the one of the model the setting points at now - the setting
        changed after the table was created.

        Args:
            model_fields: The current model's fields.
            table_info: The introspected table.

        Returns:
            (column name, what differs) pairs.
        """
        mismatches: list[tuple[str, str]] = []
        for field in model_fields.values():
            if not isinstance(field, ForeignKeyFieldInstance) or not isinstance(
                field.model_name, SwappableModelReference
            ):
                continue
            related_model = getattr(field, "related_model", None)
            if related_model is None:
                continue
            column_names = tuple(field.db_column_names)
            if len(column_names) == 1:
                foreign_key = table_info.foreign_keys.get(column_names[0])
                referenced_table = foreign_key.target_table if foreign_key is not None else None
            else:
                referenced_table = next(
                    (
                        composite_foreign_key.target_table
                        for composite_foreign_key in table_info.composite_foreign_keys
                        if set(composite_foreign_key.columns) == set(column_names)
                    ),
                    None,
                )
            if referenced_table is None or referenced_table == related_model._meta.db_table:
                continue
            setting = field.model_name.setting
            mismatches.append(
                (
                    column_names[0],
                    f'its foreign key references table "{referenced_table}", but the {setting} setting points '
                    f'at "{related_model._meta.full_name}" (table "{related_model._meta.db_table}") - the '
                    f"setting changed after the table was created; move the rows to the new model and "
                    f"repoint the foreign key with a migration of your own",
                )
            )
        return mismatches

    @staticmethod
    def _get_column_mismatches(
        model_fields: dict[str, Field[Any]], table_info: TableInfo, dialect: str
    ) -> list[tuple[str, str]]:
        """Lists the columns whose type differs from their field's in a way the observed fields
        can't carry - a relation's key column, or a type inspectdb has no field for.

        Args:
            model_fields: The current model's fields.
            table_info: The introspected table.
            dialect: The database dialect.

        Returns:
            (column name, what differs) pairs.
        """
        columns_by_name = {column.name: column for column in table_info.columns}
        mismatches: list[tuple[str, str]] = []
        for field_name, field in model_fields.items():
            if isinstance(field, ManyToManyFieldInstance):
                continue
            if isinstance(field, ForeignKeyFieldInstance):
                column_names = list(field.db_column_names)
            elif isinstance(field, RelationalField):
                continue
            else:
                column_names = [field.source_field or field_name]
            if len(column_names) != 1 or column_names[0] not in columns_by_name:
                continue
            column = columns_by_name[column_names[0]]
            declared_type = ColumnDefinitionComparer.get_declared_column_type(field, dialect)
            if declared_type is None or not ColumnDefinitionComparer.column_types_differ(
                dialect, declared_type, column
            ):
                continue
            if not isinstance(field, ForeignKeyFieldInstance) and (
                DriftStateBuilder._get_field_of_observed_type(dialect, field, column) is not None
            ):
                continue
            introspector_class = DialectRegistry.get_dialect(dialect).introspector_class
            if introspector_class is None:
                continue
            observed_type, expected_type = introspector_class.get_type_mismatch_texts(declared_type, column)
            mismatches.append((column.name, f"type is {observed_type}, the model expects {expected_type}"))
        return mismatches

    @staticmethod
    def _has_relation_index(field: ForeignKeyFieldInstance[Any], table_info: TableInfo) -> bool:
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
    def _get_relation_index_columns(fields: dict[str, Field[Any]]) -> set[tuple[str, ...]]:
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
    def _is_same_key_order(declared: Index, observed: Index, column_names: list[str]) -> bool:
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
    def _get_observed_include(
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
    def _get_declared_indexes_by_name(
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
    def _build_observed_indexes(
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
        declared_indexes_by_name = DriftStateBuilder._get_declared_indexes_by_name(
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
                    list(DriftStateBuilder._get_index_field_names(index.columns, column_to_field_name))
                )
            )
            declared_index = declared_indexes_by_name.get(index.name)
            declared_include = declared_index.include if declared_index is not None else ()
            include = DriftStateBuilder._get_observed_include(index, declared_include, column_to_field_name)
            observed_index = Index(fields=fields, name=index.name or None, include=include)
            if declared_index is not None and DriftStateBuilder._is_same_key_order(
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
                introspector_class.INDEX_CLASSES_BY_TYPE, column_to_field_name, resolved_keys=True
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
                    and DriftStateBuilder._condition_matches(declared_index.condition, index.condition_sql)
                ):
                    index_kwargs["condition"] = declared_index.condition
            if index.name:
                index_kwargs["name"] = index.name
            indexes.append(index_class(*index_args, **index_kwargs))
        return indexes

    @staticmethod
    def _get_observed_trigger(observed: Trigger, declared: Trigger | None) -> Trigger:
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
            SchemaIntrospector._normalize_event_clause(declared.on),
            declared.language,
            declared.deferrable,
            declared.initially_deferred,
        ):
            return observed
        return replace(
            observed,
            on=declared.on,
            when=declared.when if observed.when is not None and declared.when is not None else observed.when,
            body=declared.body if observed.body.strip() == declared.body.strip() else observed.body,
        )

    @staticmethod
    def _exclusion_expressions_match(
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

    @staticmethod
    def _apply_declared_uniqueness(
        model_state: ModelState, observed_fields: dict[str, Field[Any]], observed_options: dict[str, Any]
    ) -> None:
        """Gives the observed state the uniqueness the model declares, on a database without unique
        constraints (``Dialect.supports_unique_constraints``) - the schema editor creates none there,
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

    @staticmethod
    def _apply_observed_table_options(
        introspector_class: type[SchemaIntrospector],
        dialect_name: str,
        model_state: ModelState,
        table_info: TableInfo,
        options: dict[str, Any],
    ) -> None:
        """Replaces the ``Meta.table_options`` entry of the connection's dialect in ``options``
        with the table's observed options - the entries of other dialects stay as declared, since
        this database can't show them.

        Args:
            introspector_class: The connection's introspector.
            dialect_name: The connection's dialect.
            model_state: The model's declared state.
            table_info: The introspected table.
            options: The observed options of the model, changed in place.
        """
        declared_table_options: tuple[TableOptions, ...] = tuple(
            model_state.options.get(ModelOption.TABLE_OPTIONS, ())
        )
        declared = next((entry for entry in declared_table_options if entry.dialect_name == dialect_name), None)
        observed = introspector_class.get_declared_table_options(
            table_info.table_options,
            declared,
            table_info,
            DriftStateBuilder._column_to_field_name(model_state.fields),
        )
        # The observed entry takes the declared one's place - the options compare as a sequence.
        table_options = tuple(
            entry
            for declared_entry in declared_table_options
            for entry in ((observed,) if declared_entry.dialect_name == dialect_name else (declared_entry,))
            if entry is not None
        )
        if declared is None and observed is not None:
            table_options = (*table_options, observed)
        if table_options:
            options[ModelOption.TABLE_OPTIONS] = table_options
        else:
            options.pop(ModelOption.TABLE_OPTIONS, None)

    @staticmethod
    def _build_observed_options(
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
        column_to_field_name = DriftStateBuilder._column_to_field_name(model_state.fields)
        if matched_unique_indexes is None:
            matched_unique_indexes = DriftStateBuilder._match_declared_unique_indexes(
                introspector_class, model_state, table_info
            )
        matched_index_infos = [index for index, _declared in matched_unique_indexes]
        matched_index_ids = {id(index) for index in matched_index_infos}

        observed_indexes = DriftStateBuilder._build_observed_indexes(
            introspector_class,
            table_info,
            column_to_field_name,
            model_state.options.get(ModelOption.INDEXES, ()),
            matched_index_infos,
            DriftStateBuilder._get_relation_index_columns(model_state.fields),
        )
        observed_indexes.extend(declared for _index, declared in matched_unique_indexes if isinstance(declared, Index))
        # A plain single-field Meta.indexes entry is folded into its column's has_index flag -
        # an existing column index stands for the declared entry itself.
        indexed_column_names = {column.name for column in table_info.columns if column.has_index}
        column_names_by_field_name = DriftStateBuilder._field_to_column_names(model_state.fields)
        declared_single_field_indexes = DriftStateBuilder._get_declared_single_field_indexes(
            model_state, unique_is_plain
        )
        for field_name, declared_entries in declared_single_field_indexes.items():
            field_column_names = column_names_by_field_name.get(field_name, (field_name,))
            if len(field_column_names) == 1 and field_column_names[0] in indexed_column_names:
                observed_indexes.extend(
                    entry if isinstance(entry, Index) else Index(fields=tuple(entry)) for entry in declared_entries
                )
        # A single-column db_index=True index is reported as ColumnInfo.has_index - it gets the same
        # synthetic Index the declared side has.
        observed_indexes.extend(ModelState._implicit_field_indexes(observed_fields, observed_indexes))
        declared_expression_indexes_by_name = {
            name: index
            for name, index in DriftStateBuilder._get_declared_indexes_by_name(
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
                and DriftStateBuilder._exclusion_expressions_match(declared.expressions, expressions)
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
            DriftStateBuilder._get_observed_trigger(trigger, declared_triggers.get(trigger.name))
            for trigger in table_info.triggers
        ]
        if observed_triggers:
            options[ModelOption.TRIGGERS] = tuple(observed_triggers)
        else:
            options.pop(ModelOption.TRIGGERS, None)

        return options
