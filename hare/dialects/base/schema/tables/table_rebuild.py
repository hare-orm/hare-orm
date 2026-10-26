from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from hare.ddl.conditions.field_condition import FieldCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model


class TableRebuild(SchemaEditorPart):
    """A table rebuilt into a new one - for a change the database can't alter in place: the new table
    made from the model it is rebuilt to, the rows copied over from the old one with their values
    converted, the indexes and constraints made again."""

    __slots__ = ()

    @staticmethod
    def get_remake_value_conversion_sql(old_field: Field[Any], new_field: Field[Any], quoted_column: str) -> str:
        """The expression copying a column's values into a rebuilt table whose field changed type.

        Args:
            old_field: The field's previous definition.
            new_field: The field's new definition.
            quoted_column: The quoted column of the table being replaced.

        Returns:
            The column itself - the database converts a stored value to the new column type.
        """
        return quoted_column

    async def replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place.

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        qualified_table = self.editor.qualify_table_name(table_name, schema)
        await self.editor.run_sql(f"DROP TABLE {qualified_table}")
        rebuilt_table = self.editor.qualify_table_name(rebuilt_table_name, schema)
        await self.editor.run_sql(
            self.editor.RENAME_TABLE_TEMPLATE.format(old_table=rebuilt_table, new_table=self.editor.quote(table_name))
        )

    @staticmethod
    def get_remake_column_name(field: Field[Any]) -> str:
        """The DB column a field occupies.

        Args:
            field: A field of a rendered model.

        Returns:
            The column name - for a single-column FK/O2O its key column, since the relation
            field's own source_field names the shadow attribute rather than the column.
        """
        if isinstance(field, ForeignKeyFieldInstance) and len(field.db_column_names) == 1:
            return field.db_column_names[0]
        return field.source_field or field.model_field_name

    @staticmethod
    def constraint_references_field(constraint: Any, field: Field[Any]) -> bool:
        """True if a Meta.constraints entry names ``field`` among its own fields - a
        UniqueConstraint by its fields, a CheckConstraint whose condition is a Q by the fields
        the condition reads, a relation by one of its key columns' fields too. A CheckConstraint of
        raw SQL (``RawSQLTerm``) can't be read for field names."""
        field_names = {field.model_field_name, *getattr(field, "source_fields", ())}
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.check, FieldCondition):
            return not field_names.isdisjoint(constraint.check.get_referenced_field_names())
        return isinstance(constraint, UniqueConstraint) and not field_names.isdisjoint(constraint.fields)

    @staticmethod
    def index_references_field(index: Index, field: Field[Any]) -> bool:
        """True if a Meta.indexes entry keys on ``field`` - by its name, or for a relation by one of
        its key columns' fields. An expression index can't be read for field names.

        Args:
            index: The index.
            field: The field.

        Returns:
            Whether the index lists the field.
        """
        field_names = {field.model_field_name, *getattr(field, "source_fields", ())}
        return any(name.removeprefix("-") in field_names for name in index.fields)

    async def copy_table_rows(
        self, model: type[Model], new_table_name: str, old_table_name: str, column_mapping: dict[str, str]
    ) -> None:
        """Copies the rows of a table being rebuilt into its new copy.

        Args:
            model: The model rendered from the state the table is rebuilt to.
            new_table_name: The copy, empty.
            old_table_name: The table holding the rows.
            column_mapping: The SQL each column of the copy takes its value from, by the column's name.
        """
        schema = model._meta.schema
        columns_sql = ", ".join(self.editor.quote(column) for column in column_mapping)
        insert_sql = f"""INSERT INTO {self.editor.qualify_table_name(new_table_name, schema)} ({columns_sql})
                SELECT {", ".join(column_mapping.values())}
                FROM {self.editor.qualify_table_name(old_table_name, schema)}"""  # nosec B608
        await self.editor.run_sql(insert_sql)

    def get_column_mapping(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> dict[str, str]:
        """What each column of the rebuilt table is filled from: the column of the field of the same
        name in the old table - converted to its new type, its default where it turns NOT NULL - else
        the field's default, NULL without one. A generated column takes no value.

        Args:
            old_model: The model the table matches now.
            new_model: The model the table is rebuilt to.
            altered_fields: (old, new) fields paired otherwise than by name - a renamed field.

        Returns:
            The SQL of each column's value, by the column.
        """
        old_meta = old_model._meta
        new_meta = new_model._meta
        old_field_by_new_name: dict[str, Field[Any]] = {}
        for old_field, new_field in altered_fields:
            old_field_by_new_name[new_field.model_field_name] = old_field
            if isinstance(old_field, ForeignKeyFieldInstance) and isinstance(new_field, ForeignKeyFieldInstance):
                # A relation's key fields go with it.
                for old_key, new_key in zip(old_field.source_fields, new_field.source_fields, strict=True):
                    if old_key in old_meta.fields_map:
                        old_field_by_new_name.setdefault(new_key, old_meta.fields_map[old_key])
        column_definitions = self.editor.column_definitions
        column_mapping: dict[str, str] = {}
        for field_name, column in new_meta.fields_db_projection.items():
            new_field = new_meta.fields_map[field_name]
            if column_definitions.get_non_pk_generated_field_sql(new_model, new_field, column) is not None:
                continue
            # A paired field is the column it names; one matched by name only when the old table has it.
            source_field = old_field_by_new_name.get(field_name)
            if source_field is None and field_name in old_meta.fields_db_projection:
                source_field = old_meta.fields_map[field_name]
            if source_field is None:
                default_value = self.editor.column_backfill.field_backfill_sql_literal(new_field, new_model)
                column_mapping[column] = default_value if default_value is not None else "NULL"
                continue
            old_column = self.get_remake_column_name(source_field)
            copied_value_sql = self.get_remake_value_conversion_sql(
                source_field, new_field, self.editor.quote(old_column)
            )
            default_value = (
                self.editor.column_backfill.field_backfill_sql_literal(new_field, new_model)
                if source_field.null and not new_field.null
                else None
            )
            column_mapping[column] = (
                f"COALESCE({copied_value_sql}, {default_value})" if default_value is not None else copied_value_sql
            )
        return column_mapping

    async def remake_table(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> None:
        """Rebuilds a table into the one a model declares: creates it under a temporary name from the new
        model - with the same definitions ``CREATE TABLE`` gives it - copies the rows into it, puts it in
        place of the old one, then creates the objects that went with the old table from the new model: its
        indexes, the constraints the database keeps as objects of their own, its triggers. The alteration
        strategy of a database whose ALTER TABLE can't make a change in place.

        Args:
            old_model: The model the table matches now.
            new_model: The model the table is rebuilt to.
            altered_fields: (old, new) fields paired otherwise than by name - a renamed field.
        """
        table_options = new_model._meta.get_table_options(self.editor.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(new_model, self.editor.client.features)
        # The table storing the rows is the one rebuilt.
        db_table = (
            table_options.get_storage_table_name(new_model._meta.db_table)
            if table_options
            else new_model._meta.db_table
        )
        new_table_name = f"new__{db_table}"
        schema = new_model._meta.schema
        table_definitions, constraint_statements, _references, _deferred = (
            self.editor.table_creation.get_table_definitions(new_model)
        )
        qualified_new = self.editor.qualify_table_name(new_table_name, schema)
        create_sql = (
            f"CREATE {table_options.get_create_prefix_sql() if table_options else ''}TABLE {qualified_new} "
            f"({self.get_body_comment_sql(new_model, qualified_new)}{', '.join(table_definitions)})"
            f"{table_options.get_create_suffix_sql(new_model, self.editor) if table_options else ''}"
        )
        await self.editor.run_sql(create_sql)
        if table_options is not None:
            for statement in table_options.get_after_create_sqls(new_model, qualified_new, self.editor):
                await self.editor.run_sql(statement)

        column_mapping = self.get_column_mapping(old_model, new_model, altered_fields)
        if column_mapping:
            await self.copy_table_rows(new_model, new_table_name, db_table, column_mapping)

        await self.replace_table(
            db_table,
            new_table_name,
            schema,
            [new_model._meta.fields_map[field_name] for field_name in new_model._meta.fields_db_projection],
        )
        if table_options is not None:
            for statement in table_options.get_companion_table_sqls(new_model, self.editor, replaces=True):
                await self.editor.run_sql(statement)
        # Triggers, indexes and the constraints kept as objects went with the old table.
        for trigger in new_model._meta.triggers:
            await self.editor.trigger_statements.add_trigger(new_model, trigger)
        await self.editor.run_sqls(self.editor.table_creation.get_model_index_sqls(new_model))
        await self.editor.run_sqls(constraint_statements)

    async def remake_for_referencing_key_columns(self, referencing: ReferencingKeyColumns) -> bool:
        """Remakes a table whose relation key columns take the new type of the key they reference,
        where the database can't change them in place - none by default.

        Args:
            referencing: The relation key columns, with their new types.

        Returns:
            Whether the table was remade, its key columns of the new type.
        """
        return False

    async def remake_for_column_change(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> bool:
        """Remakes the table for a column change the database can't make in place - none by default.

        Args:
            model: The model rendered from the state the table changes to.
            old_field: The field before the change.
            new_field: The field after it.

        Returns:
            Whether the table was remade, the change made with it.
        """
        return False

    def get_body_comment_sql(self, model: type[Model], qualified_table: str) -> str:
        """The comment the rebuilt table's ``CREATE TABLE`` holds inside its parentheses.

        Args:
            model: The model.
            qualified_table: The rebuilt table's quoted name.

        Returns:
            The comment's SQL - none by default.
        """
        return ""
