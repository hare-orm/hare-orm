from __future__ import annotations

from collections.abc import Iterable
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.dialect_registry import DialectRegistry
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.fields.enums import OnDelete
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.constants import RELATION_FIELDS
from hare.migrations.drift.column_definition_comparer import ColumnDefinitionComparer
from hare.migrations.drift.observed.observed_indexes import ObservedIndexes

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class ObservedFields:
    """The fields a table's columns show: each column read back into a field of the observed type, with
    its database default, its relation and the relation's on_delete, and the columns that differ
    from the declared fields."""

    @staticmethod
    def expected_columns(fields: dict[str, Field[Any]]) -> set[str]:
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
    def auto_managed_through_tables(fields: dict[str, Field[Any]]) -> set[str]:
        """Names of the through tables hare itself creates for `fields`' many-to-many relations - a
        user-declared ``through=SomeModel`` table is that model's own table, tracked as such."""
        return {
            field.through
            for field in fields.values()
            if isinstance(field, ManyToManyFieldInstance) and field.through_model is None and field.through
        }

    @staticmethod
    async def get_out_of_sync_many_to_many_field_names(
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
            through_table_info = await DatabaseCatalog.inspect_table(
                connection, field.through, schema=schema, verify_exists=False
            )
            if not key_columns <= {column.name for column in through_table_info.columns}:
                out_of_sync_field_names.add(field_name)
                continue
            if (
                field.unique
                and connection.features.supports_unique_constraints
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
    def build_observed_fields(
        model_fields: dict[str, Field[Any]],
        table_info: TableInfo,
        out_of_sync_many_to_many_field_names: set[str] | frozenset[str] = frozenset(),
        declared_single_field_index_names: frozenset[str] = frozenset(),
        matched_unique_indexes: Iterable[IndexInfo] = (),
        unindexed_many_to_many_field_names: set[str] | frozenset[str] = frozenset(),
        dialect: str | Dialect | None = None,
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
                observed[field_name] = ObservedFields.get_observed_relation_field(
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
                observed_field = ObservedFields.get_field_of_observed_type(dialect, field, column) or observed_field
            if dialect is None or DatabaseCatalog.get_dialect_introspector_class(dialect).reports_nullability(column):
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
                ObservedFields.apply_observed_db_default(observed_field, field, column, dialect)
            observed[field_name] = observed_field
        return observed

    @staticmethod
    def get_field_of_observed_type(dialect: str | Dialect, field: Field[Any], column: ColumnInfo) -> Field[Any] | None:
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
    def apply_observed_db_default(
        observed_field: Field[Any], field: Field[Any], column: ColumnInfo, dialect: str | Dialect
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
    def get_observed_relation_field(
        field: ForeignKeyFieldInstance[Any],
        table_info: TableInfo,
        columns_by_name: dict[str, ColumnInfo],
        dialect: str | Dialect | None,
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
            observed_field.index = ObservedIndexes.has_relation_index(field, table_info)
        key_columns = [columns_by_name.get(column_name) for column_name in field.db_column_names]
        if not key_columns or None in key_columns or dialect is None:
            return observed_field
        present_key_columns = [column for column in key_columns if column is not None]
        if not field.pk:
            # A composite relation declared nullable makes every key column nullable.
            nullable_flags = [column.nullable for column in present_key_columns]
            observed_field.null = all(nullable_flags) if field.null else any(nullable_flags)
        if not DialectRegistry.get_dialect(dialect).features.supports_foreign_keys:
            # The database has no FOREIGN KEY constraints - db_constraint and on_delete describe
            # what hare enforces itself, nothing the database could differ in.
            if len(present_key_columns) == 1:
                ObservedFields.apply_observed_db_default(observed_field, field, present_key_columns[0], dialect)
            return observed_field
        foreign_key_on_delete = ObservedFields.get_observed_foreign_key_on_delete(field, table_info, default_schema)
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
            ObservedFields.apply_observed_db_default(observed_field, field, present_key_columns[0], dialect)
        return observed_field

    @staticmethod
    def get_observed_foreign_key_on_delete(
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
    def get_column_mismatches(
        model_fields: dict[str, Field[Any]], table_info: TableInfo, dialect: str | Dialect
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
                ObservedFields.get_field_of_observed_type(dialect, field, column) is not None
            ):
                continue
            introspector_class = DialectRegistry.get_dialect(dialect).introspector_class
            if introspector_class is None:
                continue
            observed_type, expected_type = introspector_class.get_type_mismatch_texts(declared_type, column)
            mismatches.append((column.name, f"type is {observed_type}, the model expects {expected_type}"))
        return mismatches
