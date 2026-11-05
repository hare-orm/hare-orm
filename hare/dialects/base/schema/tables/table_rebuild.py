from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import copy
from typing import Any

from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.plans.column_copy import ColumnCopy
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.plans.table_rows_copy import TableRowsCopy
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model
from hare.sql.identifiers import Identifiers


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

    def get_replace_plan(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> SchemaChangePlan:
        """Prepare the replacement commands without dropping or renaming a table."""
        qualified_table = self.editor.get_schema_table_sql(table_name, schema)
        return SchemaChangePlan(
            (
                SchemaStatement(f"DROP TABLE {qualified_table}", frozenset({SchemaEffect.REPLACE_TABLE})),
                SchemaStatement(self.editor.get_table_rename_sql(rebuilt_table_name, table_name, schema)),
            )
        )

    @staticmethod
    def get_rebuilt_table_name(table_name: str) -> str:
        """Choose the staging name once when compiling a rebuild plan."""
        return Identifiers.get_within_limit(f"new__{table_name}")

    def get_storage_table_name(self, model: type[Model]) -> str:
        """The physical table selected by this model's declared storage options."""
        options = model._meta.get_table_options(self.editor.client.dialect)
        return options.get_storage_table_name(model._meta.db_table) if options else model._meta.db_table

    def get_copy_source_table(self, model: type[Model]) -> str:
        """Read rows from the old declaration, before applying any storage-name change."""
        return self.get_storage_table_name(model)

    def get_before_replace_plan(self, old_model: type[Model], new_model: type[Model]) -> SchemaChangePlan:
        """Prepare dependent objects for replacement after the row copy succeeds."""
        return SchemaChangePlan(())

    def get_staging_plan(
        self, model: type[Model], rebuilt_table_name: str, preparation: SchemaChangePlan
    ) -> SchemaChangePlan:
        """Scope preparation after CREATE has acquired the replacement table."""
        return preparation

    def get_rebuild_finalization_plan(
        self, old_model: type[Model], new_model: type[Model], rebuilt_table_name: str
    ) -> SchemaChangePlan:
        """Finish dialect-specific objects and release any retained previous table."""
        return SchemaChangePlan(())

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

    def get_copy_plan(
        self, model: type[Model], new_table_name: str, source_sql: str, columns: tuple[ColumnCopy, ...]
    ) -> SchemaChangePlan:
        """Prepare a row copy; dialects may wrap it in a temporary-resource scope."""
        copy = TableRowsCopy(
            model,
            self.editor.qualify_table_name(new_table_name, model._meta.schema),
            source_sql,
            columns,
        )
        if any(column.clock_field is not None for column in columns):
            return SchemaChangePlan((copy,))
        effects = {SchemaEffect.REWRITE_ROWS}
        if any(column.default_sql is not None for column in columns):
            effects.add(SchemaEffect.BACKFILL_ROWS)
        return SchemaChangePlan((SchemaStatement(copy.get_sql(self.editor), frozenset(effects)),))

    def get_copy_columns(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> tuple[ColumnCopy, ...]:
        """What each column of the rebuilt table is filled from: the column of the field of the same
        name in the old table - converted to its new type, its default where it turns NOT NULL - else
        the field's default, NULL without one. A generated column takes no value.

        Args:
            old_model: The model the table matches now.
            new_model: The model the table is rebuilt to.
            altered_fields: (old, new) fields paired otherwise than by name - a renamed field.

        Returns:
            Prepared source expressions and defaults, with clocks deferred until execution.
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
        columns: list[ColumnCopy] = []
        for field_name, column in new_meta.fields_db_projection.items():
            new_field = new_meta.fields_map[field_name]
            if column_definitions.get_non_pk_generated_field_sql(new_model, new_field, column) is not None:
                continue
            # A paired field is the column it names; one matched by name only when the old table has it.
            source_field = old_field_by_new_name.get(field_name)
            if source_field is None and field_name in old_meta.fields_db_projection:
                source_field = old_meta.fields_map[field_name]
            source_sql = None
            if source_field is not None:
                source_sql = self.get_remake_value_conversion_sql(
                    source_field, new_field, self.editor.quote(self.get_remake_column_name(source_field))
                )
            needs_default = source_field is None or (source_field.null and not new_field.null)
            clock_field = None
            default_sql = None
            if needs_default:
                backfill = self.editor.column_backfill
                if (
                    new_field.default is None
                    and not new_field.has_db_default()
                    and backfill.is_auto_now_field(new_field)
                ):
                    clock_field = copy(new_field)
                else:
                    default_sql = backfill.field_backfill_sql_literal(new_field, new_model)
            columns.append(ColumnCopy(self.editor.quote(column), source_sql, default_sql, clock_field))
        return tuple(columns)

    def get_rebuild_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> SchemaChangePlan:
        """Compile creation, copying, replacement and dependent objects before any DDL.

        Unsupported declarations and rebuild defaults fail during preparation. The
        plan retains the final before/after declarations, while the replacement's
        temporary name is confined to its prepared commands.

        Args:
            old_model: The model the table matches now.
            new_model: The model the table is rebuilt to, including its dependent objects.
            altered_fields: Old and new fields paired otherwise than by name, such as a renamed field.
        """
        table_options = new_model._meta.get_table_options(self.editor.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(new_model, self.editor.client.features)
        db_table = self.get_storage_table_name(new_model)
        old_db_table = self.get_storage_table_name(old_model)
        new_table_name = self.get_rebuilt_table_name(db_table)
        schema = new_model._meta.schema
        definition = self.editor.table_creation.get_definition(new_model, storage_table_name=new_table_name)
        steps: list[SchemaStep] = [SchemaStatement(definition.get_create_sql(self.editor.TABLE_CREATE_TEMPLATE))]
        preparation: list[SchemaStep] = []
        if table_options is not None:
            preparation.extend(
                SchemaStatement(sql)
                for sql in table_options.get_after_create_sqls(new_model, new_table_name, self.editor)
            )
        columns = self.get_copy_columns(old_model, new_model, altered_fields)
        if columns:
            preparation.extend(
                self.get_copy_plan(
                    new_model,
                    new_table_name,
                    self.editor.qualify_table_name(self.get_copy_source_table(old_model), old_model._meta.schema),
                    columns,
                ).steps
            )
        steps.extend(self.get_staging_plan(new_model, new_table_name, SchemaChangePlan(tuple(preparation))).steps)
        steps.extend(self.get_before_replace_plan(old_model, new_model).steps)
        steps.extend(
            self.get_replace_plan(
                old_db_table,
                new_table_name,
                schema,
                [new_model._meta.fields_map[name] for name in new_model._meta.fields_db_projection],
            ).steps
        )
        if old_db_table != db_table:
            steps.append(SchemaStatement(self.editor.get_table_rename_sql(old_db_table, db_table, schema)))
        if table_options is not None:
            steps.extend(
                SchemaStatement(sql)
                for sql in table_options.get_companion_table_sqls(new_model, self.editor, replaces=True)
            )
        steps.extend(
            SchemaStatement(sql)
            for trigger in new_model._meta.triggers
            for sql in self.editor.trigger_statements.get_trigger_create_sqls(new_model, trigger)
        )
        steps.extend(SchemaStatement(sql) for sql in self.editor.table_creation.get_model_index_sqls(new_model))
        steps.extend(SchemaStatement(sql) for sql in definition.constraints)
        steps.extend(self.get_rebuild_finalization_plan(old_model, new_model, new_table_name).steps)
        return SchemaChangePlan(
            tuple(steps),
            before=self.editor.table_creation.get_definition(old_model),
            after=self.editor.table_creation.get_definition(new_model),
        )

    def get_referencing_key_rebuild_plan(self, referencing: ReferencingKeyColumns) -> SchemaChangePlan | None:
        """Choose a full rebuild when a referencing key cannot be altered in place."""
        return None

    def get_column_change_rebuild_plan(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any]
    ) -> SchemaChangePlan | None:
        """Select a prepared rebuild when a column change cannot be made in place."""
        return None
