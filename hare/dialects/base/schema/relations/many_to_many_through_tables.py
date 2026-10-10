from __future__ import annotations

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model


class ManyToManyThroughTables(SchemaEditorPart):
    """The through tables of many-to-many relations when a relation changes: the table altered or
    replaced, its indexes kept, its rows moved between an automatic and a declared through model."""

    __slots__ = ()

    async def alter_many_to_many_field(
        self,
        model: type[Model],
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> None:
        if old_field.through_model is not None and new_field.through_model is not None:
            # A ManyToManyField(through=SomeModel)'s table/columns are SomeModel's own - any
            # rename/alteration of them goes through SomeModel's own AlterField/RenameField
            # operations, not this M2M-specific through-table path.
            return
        if old_field.through_model is not None or new_field.through_model is not None:
            await self.move_many_to_many_rows_between_through_tables(model, old_field, new_field)
            return
        schema = model._meta.schema
        if old_field.through != new_field.through:
            await self.editor.run_sql(
                self.editor.RENAME_TABLE_TEMPLATE.format(
                    old_table=self.editor.qualify_table_name(old_field.through, schema),
                    new_table=self.editor.quote(new_field.through),
                )
            )

        qualified_through = self.editor.qualify_table_name(new_field.through, schema)
        for old_key, new_key in zip(old_field.forward_keys, new_field.forward_keys, strict=True):
            if old_key != new_key:
                await self.editor.run_sql(
                    self.editor.RENAME_FIELD_TEMPLATE.format(
                        table=qualified_through,
                        old_column=self.editor.quote(old_key),
                        new_column=self.editor.quote(new_key),
                    )
                )

        for old_key, new_key in zip(old_field.backward_keys, new_field.backward_keys, strict=True):
            if old_key != new_key:
                await self.editor.run_sql(
                    self.editor.RENAME_FIELD_TEMPLATE.format(
                        table=qualified_through,
                        old_column=self.editor.quote(old_key),
                        new_column=self.editor.quote(new_key),
                    )
                )

        # Before the rebuild below: SQLite's rebuild recreates the table with the new definition's
        # indexes, which a later diff against the old ones would drop and create a second time.
        await self.alter_many_to_many_through_indexes(schema, old_field, new_field)
        if self.editor.foreign_key_rebuild.foreign_key_changed(old_field, new_field):
            await self.editor.foreign_key_rebuild.rebuild_many_to_many_through_foreign_keys(model, new_field)

    async def alter_many_to_many_through_indexes(
        self,
        schema: str | None,
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> None:
        """Drops the automatic through table's plain key indexes the old definition had and the
        new one doesn't, and creates the new ones - an index is named after its table and
        columns, so a renamed table or key column moves it too.

        Args:
            schema: The through table's schema.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """
        old_index_names = {
            GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, old_field.through, index_keys)
            for index_keys in old_field.get_through_index_keys()
        }
        new_index_keys_by_name = {
            GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, new_field.through, index_keys): index_keys
            for index_keys in new_field.get_through_index_keys()
        }
        for index_name in sorted(old_index_names - new_index_keys_by_name.keys()):
            await self.editor.run_sql(
                self.editor.DROP_INDEX_TEMPLATE.format(
                    name=self.editor.qualify_table_name(index_name, schema),
                    table=self.editor.qualify_table_name(new_field.through, schema),
                )
            )
        for index_name, index_keys in new_index_keys_by_name.items():
            if index_name not in old_index_names:
                await self.editor.run_sql(
                    self.editor.index_statements.get_table_index_sql(new_field.through, index_keys, schema=schema)
                )

    async def move_many_to_many_rows_between_through_tables(
        self,
        model: type[Model],
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> None:
        """Carries an M2M relation's rows over between hare's automatic through table and a
        through model's table (either direction).

        The through model's own table is created/dropped by its CreateModel/DeleteModel - only the
        automatic table is created or dropped here.

        Args:
            model: The model owning the M2M field, rendered from the new state.
            old_field: The relation's current definition.
            new_field: The relation's new definition.
        """
        if new_field.through_model is None:
            table_definition = self.editor.table_creation.get_many_to_many_table_definition(model, new_field)
            if table_definition:
                await self.editor.run_sql(table_definition)
        old_table = self.editor.qualify_table_name(old_field.through, old_field.through_schema or model._meta.schema)
        new_table = self.editor.qualify_table_name(new_field.through, new_field.through_schema or model._meta.schema)
        old_columns = ", ".join(self.editor.quote(key) for key in (*old_field.backward_keys, *old_field.forward_keys))
        new_column_names = [*new_field.backward_keys, *new_field.forward_keys]
        default_values: list[str] = []
        if new_field.through_model is not None:
            for column_name, default_value in (await self.get_through_model_default_values(new_field)).items():
                if column_name not in new_column_names:
                    new_column_names.append(column_name)
                    default_values.append(default_value)
        new_columns = ", ".join(self.editor.quote(column_name) for column_name in new_column_names)
        selected_values = ", ".join([old_columns, *default_values])
        # The automatic table holds each pair once - a through model may repeat one.
        distinct = "DISTINCT " if new_field.through_model is None else ""
        await self.editor.run_sql(
            f"INSERT INTO {new_table} ({new_columns}) SELECT {distinct}{selected_values} FROM {old_table}"  # nosec B608
        )
        if old_field.through_model is None:
            await self.editor.run_sql(self.editor.DELETE_TABLE_TEMPLATE.format(table=old_table))

    async def get_through_model_default_values(
        self, many_to_many_field: ManyToManyFieldInstance[Model]
    ) -> dict[str, str]:
        """The Python defaults of a through model's own columns, for the rows carried over into
        its table. A callable default is evaluated once - every carried-over row gets that value.

        Args:
            many_to_many_field: An M2M field declared with ``through=SomeModel``, relations initialized.

        Returns:
            Column name -> the default as a SQL literal, for each column with a Python default
            (or the current time of an auto_now/auto_now_add field) and no database default.

        Raises:
            ConfigurationError: A unique column's default is callable - one value computed for
                every row can't be unique.
        """
        through_model = many_to_many_field.through_model_class
        if through_model is None:
            return {}
        unique_constraint_field_names = {
            field_name
            for constraint in through_model._meta.constraints
            if isinstance(constraint, UniqueConstraint)
            for field_name in constraint.fields
        }
        default_values: dict[str, str] = {}
        for field_name, column_name in through_model._meta.fields_db_projection.items():
            field = through_model._meta.fields_map[field_name]
            if field.pk or field.generated or field.has_db_default():
                continue
            if field.default is None and not self.editor.column_backfill.is_auto_now_field(field):
                continue
            if callable(field.default) and (field.unique or field_name in unique_constraint_field_names):
                raise ConfigurationError(
                    f"Can't carry the rows of {many_to_many_field.model_field_name!r} over into "
                    f"{through_model.__name__}'s table - its unique field {field_name!r} has a callable "
                    "default, and computing it once would give every row the same value. Give it a "
                    "db_default, or fill the through table yourself in a RunPython step."
                )
            default_values[column_name] = await self.editor.column_backfill.added_column_backfill_sql_literal(
                field, through_model
            )
        return default_values
