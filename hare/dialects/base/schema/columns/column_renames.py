from __future__ import annotations

from typing import Any

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model


class ColumnRenames(SchemaEditorPart):
    """The columns a rename changes - renamed in place by default (``ALTER TABLE ... RENAME
    COLUMN``)."""

    __slots__ = ()

    @staticmethod
    def get_column_names(field: Field[Any]) -> tuple[str, ...]:
        """The columns a field of a rendered model occupies.

        Args:
            field: The field.

        Returns:
            The column names, one per key column of a relation.
        """
        if isinstance(field, ForeignKeyFieldInstance) and field.db_column_names:
            return field.db_column_names
        return (field.source_field or field.model_field_name,)

    def get_model_column_rename_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_pairs: list[tuple[Field[Any], Field[Any]]],
    ) -> SchemaChangePlan:
        """Prepares the rename of every changed field column before executing any DDL.

        Args:
            old_model: The model before the rename.
            new_model: The model after it.
            field_pairs: The (old, new) fields whose columns change.

        Returns:
            The column changes, or a table rebuild whose effects include ``REPLACE_TABLE``.
        """
        steps = []
        for old_field, new_field in field_pairs:
            for old_column, new_column in zip(
                self.get_column_names(old_field), self.get_column_names(new_field), strict=True
            ):
                if old_column != new_column:
                    steps.append(
                        SchemaStatement(
                            self.editor.get_column_change_sql(
                                new_model,
                                self.editor.RENAME_FIELD_TEMPLATE,
                                old_column=self.editor.quote(old_column),
                                new_column=self.editor.quote(new_column),
                            )
                        )
                    )
        return SchemaChangePlan(tuple(steps))

    def get_table_column_rename_plan(
        self, table_name: str, schema: str | None, old_columns: tuple[str, ...], new_columns: tuple[str, ...]
    ) -> SchemaChangePlan:
        """Prepares each changed column name of an explicitly selected table.

        Args:
            table_name: The table.
            schema: Its schema, None for the default one.
            old_columns: The columns before.
            new_columns: The same columns after, in the same order.

        Returns:
            The prepared column renames in their declared order.
        """
        editor = self.editor
        qualified_table = editor.get_schema_table_sql(table_name, schema)
        steps = []
        for old_column, new_column in zip(old_columns, new_columns, strict=True):
            if old_column == new_column:
                continue
            steps.append(
                SchemaStatement(
                    editor.RENAME_FIELD_TEMPLATE.format(
                        table=qualified_table, old_column=editor.quote(old_column), new_column=editor.quote(new_column)
                    )
                )
            )
        return SchemaChangePlan(tuple(steps))
