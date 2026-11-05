from __future__ import annotations

from collections import Counter
from dataclasses import replace
from itertools import product
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.many_to_many_field import ManyToManyField
from hare.inspectdb.constants import FOREIGN_KEY_NO_ACTION
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.inspectdb.introspection.column_info import ColumnInfo
    from hare.inspectdb.introspection.index_info import IndexInfo
    from hare.inspectdb.introspection.table_info import TableInfo


class ManyToManyTables:
    """Rebuild automatic through tables only when their catalog properties fit a field.

    Other junction tables remain explicit models. Unsupported catalog properties stay
    available to the ordinary model mapper and its diagnostics.
    """

    def __init__(
        self,
        table_infos: list[TableInfo],
        class_references: dict[str, str],
        introspector_class: type[SchemaIntrospector] = SchemaIntrospector,
    ) -> None:
        """
        Args:
            table_infos: The tables of the run.
            class_references: The ``"<app>.<Class>"`` of each table's model, by table name.
            introspector_class: Normalizes identity-specific storage types for references.
        """
        #: The many-to-many fields each table's model gets, by table name: preferred attribute name
        #: and field.
        self.fields_by_table: dict[str, list[tuple[str, Field[Any]]]] = {}
        #: The note of each through table rebuilt so, by table name.
        self.notes_by_table: dict[str, str] = {}
        tables_by_name = {table_info.name: table_info for table_info in table_infos}
        referenced_tables = {
            foreign_key.target_table for table_info in table_infos for foreign_key in table_info.foreign_keys.values()
        } | {
            foreign_key.target_table for table_info in table_infos for foreign_key in table_info.composite_foreign_keys
        }
        for table_info in table_infos:
            if table_info.name in referenced_tables:
                continue
            sides = self.get_sides(table_info, tables_by_name, introspector_class)
            if sides is None:
                continue
            (owner_column, owner_table), (other_column, other_table) = sides
            # hare names a through table <owner table>_<field> - the field's name is the rest.
            field_name = table_info.name.removeprefix(f"{owner_table}_")
            attribute_name = ModelNaming.get_field_name(
                field_name if field_name and field_name != table_info.name else other_table
            )
            field = None
            for unique, db_index in product((True, False), repeat=2):
                candidate = cast(
                    "ManyToManyFieldInstance[Any]",
                    ManyToManyField(
                        class_references[other_table],
                        through=table_info.name,
                        forward_key=other_column,
                        backward_key=owner_column,
                        related_name=ModelNaming.get_field_name(f"{table_info.name}_set"),
                        unique=unique,
                        db_index=db_index,
                        on_delete=table_info.foreign_keys[owner_column].on_delete,
                        description=table_info.table_description,
                    ),
                )
                candidate.backward_keys = (owner_column,)
                candidate.forward_keys = (other_column,)
                if self.has_matching_indexes(table_info, candidate):
                    field = candidate
                    break
            if field is None:
                continue
            self.fields_by_table.setdefault(owner_table, []).append((attribute_name, field))
            owner_class_name = class_references[owner_table].rpartition(".")[2]
            self.notes_by_table[table_info.name] = (
                f"# NOTE: table {table_info.name!r} is the through table of the ManyToManyField "
                f"{owner_class_name}.{attribute_name}."
            )

    @classmethod
    def get_sides(
        cls,
        table_info: TableInfo,
        tables_by_name: dict[str, TableInfo],
        introspector_class: type[SchemaIntrospector] = SchemaIntrospector,
    ) -> tuple[tuple[str, str], tuple[str, str]] | None:
        """Find two primary-key references whose columns and rules an automatic table preserves.

        Args:
            table_info: The table.
            tables_by_name: The tables of the run.
            introspector_class: Supplies reference-column storage types.

        Returns:
            The column and the target table of each side, in the order of the columns; None for a
            table of any other shape.
        """
        if not table_info.is_many_to_many_junction_shape or table_info.composite_foreign_keys:
            return None
        if len(table_info.foreign_keys) != 2 or len(table_info.columns) != 2:
            return None
        if any(
            (
                table_info.diagnostics,
                table_info.triggers,
                table_info.exclusion_constraints,
                table_info.check_constraints,
                table_info.not_valid_constraint_names,
                table_info.table_options,
                table_info.row_level_security,
                table_info.policies,
                table_info.grants,
                table_info.sequences,
                table_info.sequences_of_other_tables,
                table_info.primary_key_without_overlaps,
            )
        ):
            return None
        sides = []
        on_delete = next(iter(table_info.foreign_keys.values())).on_delete
        if on_delete not in {OnDelete.CASCADE, OnDelete.RESTRICT, OnDelete.NO_ACTION, OnDelete.SET_NULL}:
            return None
        for column in table_info.columns:
            foreign_key = table_info.foreign_keys.get(column.name)
            if (
                foreign_key is None
                or foreign_key.to_field is not None
                or foreign_key.target_table not in tables_by_name
                or foreign_key.on_delete != on_delete
                or foreign_key.on_update != FOREIGN_KEY_NO_ACTION
                or foreign_key.deferral
            ):
                return None
            target = tables_by_name[foreign_key.target_table]
            target_schema = foreign_key.target_schema if foreign_key.target_schema is not None else table_info.schema
            primary_keys = [target_column for target_column in target.columns if target_column.is_pk]
            if (
                target.is_many_to_many_junction_shape
                or target_schema != target.schema
                or len(primary_keys) != 1
                or foreign_key.target_column != primary_keys[0].name
                or column.nullable != (on_delete == OnDelete.SET_NULL)
                or column.is_pk
                or column.is_unique
                or column.pk_position is not None
                or column.db_default is not None
                or column.description is not None
                or column.generated_expression is not None
                or column.identity_generation is not None
                or column.todo_reason is not None
                or cls.get_storage_column(column, introspector_class)
                != cls.get_storage_column(primary_keys[0], introspector_class)
            ):
                return None
            sides.append((column.name, foreign_key.target_table))
        if table_info.schema != tables_by_name[sides[0][1]].schema:
            return None
        return sides[0], sides[1]

    @staticmethod
    def get_storage_column(
        column: ColumnInfo, introspector_class: type[SchemaIntrospector] = SchemaIntrospector
    ) -> ColumnInfo:
        """Compare catalog types while ignoring the target key's identity and value-generation rules."""
        return replace(
            column,
            name="",
            db_type=introspector_class.get_referenced_column_type(column),
            nullable=False,
            is_pk=False,
            is_unique=False,
            pk_position=None,
            has_index=False,
            db_default=None,
            description=None,
            identity_generation=None,
            generates_key=False,
        )

    @staticmethod
    def is_plain_index(index: IndexInfo) -> bool:
        """Whether an automatic through-table index can retain all reported index properties."""
        return not (
            index.is_special()
            or index.storage_parameters
            or index.deferrable
            or index.initially_deferred
            or index.nulls_not_distinct
            or index.without_overlaps
            or not index.is_valid
            or index.unrepresentable_properties
            or any(order not in {"ASC", "ASC NULLS LAST"} for order in index.key_orders)
        )

    @classmethod
    def has_matching_indexes(cls, table: TableInfo, field: ManyToManyFieldInstance[Any]) -> bool:
        """Compare the entire index multiset, including names and column order."""
        indexes = [*table.indexes, *table.column_indexes]
        if not all(cls.is_plain_index(index) for index in indexes):
            return False
        expected = []
        if field.unique:
            keys = (*field.backward_keys, *field.forward_keys)
            expected.append(
                (GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_INDEX, table.name, keys), keys, True)
            )
        for keys in field.get_through_index_keys():
            expected.append((GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, table.name, keys), keys, False))
        return Counter(expected) == Counter((index.name, tuple(index.columns), index.is_unique) for index in indexes)
