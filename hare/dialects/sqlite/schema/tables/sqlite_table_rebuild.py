from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.sqlite.schema.constants import (
    SQLITE_AUTOINCREMENT_KEYWORD,
    SQLITE_BOOLEAN_TO_TEXT_SQL,
    SQLITE_COPY_REBUILT_TABLE_SEQUENCE_SQL,
    SQLITE_DATE_TO_AWARE_DATETIME_SQL,
    SQLITE_DATE_TO_NAIVE_DATETIME_SQL,
    SQLITE_DATETIME_TO_DATE_SQL,
    SQLITE_DISABLE_LEGACY_ALTER_TABLE_SQL,
    SQLITE_ENABLE_LEGACY_ALTER_TABLE_SQL,
    SQLITE_FLOAT_TO_DECIMAL_SQL,
    SQLITE_INTEGER_TO_DECIMAL_SQL,
    SQLITE_NUMBER_TO_BOOLEAN_SQL,
    SQLITE_NUMBER_TO_INTEGER_SQL,
    SQLITE_RAISE_REBUILT_TABLE_SEQUENCE_SQL,
    SQLITE_TEXT_TO_BOOLEAN_SQL,
)
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model import Model


class SqliteTableRebuild(TableRebuild):
    """TableRebuild as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    @staticmethod
    def get_remake_value_conversion_sql(old_field: Field[Any], new_field: Field[Any], quoted_column: str) -> str:
        """The expression copying a column's values into a rebuilt table whose field changed type.

        Args:
            old_field: The field's previous definition.
            new_field: The field's new definition.
            quoted_column: The quoted column of the table being replaced.

        Returns:
            An expression converting each stored value to the new field's storage format, or the
            column itself when the format stays the same.
        """
        number_field_classes = (IntField, FloatField, DecimalField)
        text_field_classes = (CharField, TextField)
        template = None
        template_arguments: dict[str, Any] = {}
        if isinstance(new_field, BooleanField) and not isinstance(old_field, BooleanField):
            if isinstance(old_field, number_field_classes):
                template = SQLITE_NUMBER_TO_BOOLEAN_SQL
            elif isinstance(old_field, text_field_classes):
                template = SQLITE_TEXT_TO_BOOLEAN_SQL
        elif isinstance(old_field, BooleanField) and isinstance(new_field, text_field_classes):
            template = SQLITE_BOOLEAN_TO_TEXT_SQL
        elif isinstance(new_field, IntField) and isinstance(old_field, (FloatField, DecimalField)):
            template = SQLITE_NUMBER_TO_INTEGER_SQL
        elif isinstance(new_field, DecimalField) and isinstance(old_field, (IntField, BooleanField)):
            template = SQLITE_INTEGER_TO_DECIMAL_SQL
            template_arguments["fraction"] = (
                f" || '.{'0' * new_field.decimal_places}'" if new_field.decimal_places else ""
            )
        elif isinstance(new_field, DecimalField) and isinstance(old_field, FloatField):
            template = SQLITE_FLOAT_TO_DECIMAL_SQL
            template_arguments["decimal_places"] = new_field.decimal_places
        elif isinstance(new_field, DateField) and isinstance(old_field, DatetimeField):
            template = SQLITE_DATETIME_TO_DATE_SQL
        elif isinstance(new_field, DatetimeField) and isinstance(old_field, DateField):
            template = (
                SQLITE_DATE_TO_AWARE_DATETIME_SQL if Timezone.get_use_timezone() else SQLITE_DATE_TO_NAIVE_DATETIME_SQL
            )
        if template is None:
            return quoted_column
        return template.format(column=quoted_column, **template_arguments)

    async def replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place - an AUTOINCREMENT primary
        key goes on from the highest id the replaced table ever issued, and the rename leaves
        other tables' references to the table as they are (``legacy_alter_table``).

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        keeps_sequence = self.uses_autoincrement(fields)
        qualified_table = self.editor.qualify_table_name(table_name, schema)
        qualified_rebuilt_table = self.editor.qualify_table_name(rebuilt_table_name, schema)
        if keeps_sequence:
            sequence_table_names = {
                "old_table": self.editor.client.dialect.literals.get_string_literal_sql(table_name),
                "new_table": self.editor.client.dialect.literals.get_string_literal_sql(rebuilt_table_name),
            }
            await self.editor.run_sql(SQLITE_RAISE_REBUILT_TABLE_SEQUENCE_SQL.format(**sequence_table_names))
            await self.editor.run_sql(SQLITE_COPY_REBUILT_TABLE_SEQUENCE_SQL.format(**sequence_table_names))
        await self.editor.run_sql(f"DROP TABLE {qualified_table}")
        await self.editor.run_sql(SQLITE_ENABLE_LEGACY_ALTER_TABLE_SQL)
        try:
            await self.editor.run_sql(f"ALTER TABLE {qualified_rebuilt_table} RENAME TO {qualified_table}")
        finally:
            await self.editor.run_sql(SQLITE_DISABLE_LEGACY_ALTER_TABLE_SQL)

    async def get_index_names(self, table_name: str, schema: str | None) -> set[str] | None:
        prefix = f"{self.editor.quote(schema)}." if schema else ""
        _, indexes = await self.editor.client.execute(f"PRAGMA {prefix}index_list({self.editor.quote(table_name)})")
        return {index["name"] for index in indexes}

    async def remake_table(
        self,
        model: type[Model],
        create_field: Field[Any] | None = None,
        delete_field: Field[Any] | None = None,
        alter_fields: list[tuple[Field[Any], Field[Any]]] | None = None,
        delete_constraint: Any = None,
        added_column_name: str | None = None,
    ) -> None:
        """Rebuilds the table - an index kept in a table of its own is dropped first and created
        again with the indexes, filled from the rebuilt table: its triggers go with the old table."""
        await self.editor.drop_own_table_indexes(model, self.editor.get_own_table_indexes(model))
        await super().remake_table(
            model,
            create_field=create_field,
            delete_field=delete_field,
            alter_fields=alter_fields,
            delete_constraint=delete_constraint,
            added_column_name=added_column_name,
        )

    def uses_autoincrement(self, fields: Iterable[Field[Any]]) -> bool:
        """Whether a table built from ``fields`` has an AUTOINCREMENT primary key.

        Args:
            fields: The fields of the table's columns.

        Returns:
            True when its generated primary key never reuses ids.
        """
        for field in fields:
            if field.pk and field.generated and not isinstance(field, ForeignKeyFieldInstance):
                generated_sql = field.get_generated_sql(self.editor.client.dialect) or ""
                return SQLITE_AUTOINCREMENT_KEYWORD in generated_sql.upper()
        return False
