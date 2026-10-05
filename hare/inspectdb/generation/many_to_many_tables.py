from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.fields.relations.fields.many_to_many_field import ManyToManyField
from hare.inspectdb.generation.model_naming import ModelNaming

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.inspectdb.introspection.table_info import TableInfo


class ManyToManyTables:
    """The through tables of many-to-many relations among the tables of one inspectdb run - each
    rebuilt as a ``ManyToManyField`` on the model of its first side, ``through=`` naming the table, so
    the generated module drifts clean; the table itself gets a note instead of a class."""

    def __init__(self, table_infos: list[TableInfo], class_references: dict[str, str]) -> None:
        """
        Args:
            table_infos: The tables of the run.
            class_references: The ``"<app>.<Class>"`` of each table's model, by table name.
        """
        #: The many-to-many fields each table's model gets, by table name: preferred attribute name
        #: and field.
        self.fields_by_table: dict[str, list[tuple[str, Field[Any]]]] = {}
        #: The note of each through table rebuilt so, by table name.
        self.notes_by_table: dict[str, str] = {}
        tables_by_name = {table_info.name: table_info for table_info in table_infos}
        for table_info in table_infos:
            sides = self.get_sides(table_info, tables_by_name)
            if sides is None:
                continue
            (owner_column, owner_table), (other_column, other_table) = sides
            # hare names a through table <owner table>_<field> - the field's name is the rest.
            field_name = table_info.name.removeprefix(f"{owner_table}_")
            attribute_name = ModelNaming.get_field_name(
                field_name if field_name and field_name != table_info.name else other_table
            )
            # A pair is linked once only where a unique index over both columns says so.
            unique = any(
                index.is_unique and set(index.columns) == {owner_column, other_column} for index in table_info.indexes
            )
            field = cast(
                "Field[Any]",
                ManyToManyField(
                    class_references[other_table],
                    through=table_info.name,
                    forward_key=other_column,
                    backward_key=owner_column,
                    related_name=ModelNaming.get_field_name(f"{table_info.name}_set"),
                    unique=unique,
                ),
            )
            self.fields_by_table.setdefault(owner_table, []).append((attribute_name, field))
            owner_class_name = class_references[owner_table].rpartition(".")[2]
            self.notes_by_table[table_info.name] = (
                f"# NOTE: table {table_info.name!r} is the through table of the ManyToManyField "
                f"{owner_class_name}.{attribute_name}."
            )

    @staticmethod
    def get_sides(
        table_info: TableInfo, tables_by_name: dict[str, TableInfo]
    ) -> tuple[tuple[str, str], tuple[str, str]] | None:
        """The two sides of a through table - two single-column foreign keys to the primary keys of
        tables of the run, and no other column.

        Args:
            table_info: The table.
            tables_by_name: The tables of the run.

        Returns:
            The column and the target table of each side, in the order of the columns; None for a
            table of any other shape.
        """
        if not table_info.is_many_to_many_junction_shape or table_info.composite_foreign_keys:
            return None
        if len(table_info.foreign_keys) != 2 or len(table_info.columns) != 2:
            return None
        sides = []
        for column in table_info.columns:
            foreign_key = table_info.foreign_keys.get(column.name)
            if (
                foreign_key is None
                or foreign_key.to_field is not None
                or foreign_key.target_table not in tables_by_name
            ):
                return None
            sides.append((column.name, foreign_key.target_table))
        return sides[0], sides[1]
