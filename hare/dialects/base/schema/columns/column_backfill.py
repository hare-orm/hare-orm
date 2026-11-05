from __future__ import annotations

import inspect
from copy import copy
from typing import Any, cast

from hare.dialects.base.schema.constants import BACKFILL_WITHOUT_ROW_IDENTITY_MESSAGE
from hare.dialects.base.schema.plans.column_default_backfill import ColumnDefaultBackfill
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.stored_value_rewrite import StoredValueRewrite
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import ConfigurationError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.models import Model


class ColumnBackfill(SchemaEditorPart):
    """Values written into the rows of an added or altered column: the field's default, auto_now's
    moment, batches of a backfill, and the NOT NULL set once every row has a value."""

    __slots__ = ()

    def backfill_default_sql_literal(self, field: Field[Any], default_value: Any, model: type[Model]) -> str:
        """Renders a field's Python ``default`` as the SQL literal existing rows are filled with.

        Raises:
            ConfigurationError: The default is a callable - computed once, it would give every row
                one value.
        """
        if callable(default_value):
            raise ConfigurationError(
                f"Can't backfill {model.__name__}.{field.model_field_name} during this schema "
                "change - its default is a callable, and computing it once here would give "
                "every existing row the same value instead of each row its own. Provide an "
                "explicit non-callable default for this migration, or backfill the column "
                "yourself in a RunPython step first."
            )
        return self.editor.client.dialect.literals.get_literal_sql(
            self.editor.client.dialect.types.get_db_value(field, default_value, model)
        )

    @staticmethod
    def is_auto_now_field(field: Field[Any]) -> bool:
        """Whether ``field`` stamps the current time itself (``auto_now``/``auto_now_add``).

        Args:
            field: The field to check.

        Returns:
            True for an auto_now/auto_now_add DatetimeField or TimeField.
        """
        return isinstance(field, DatetimeField | TimeField) and (field.auto_now or field.auto_now_add)

    @classmethod
    def needs_added_column_backfill(cls, field: Field[Any]) -> bool:
        """Whether adding ``field``'s column to a table that may already hold rows needs those
        rows filled first - a NOT NULL column whose only default lives in Python (the database
        has no value to give existing rows): a ``default``, or the current time of an
        ``auto_now``/``auto_now_add`` field."""
        return (
            not field.null
            and not field.pk
            and not field.generated
            and not field.has_db_default()
            and (field.default is not None or cls.is_auto_now_field(field))
        )

    def auto_now_backfill_sql_literal(self, field: Field[Any], model: type[Model]) -> str:
        """The current time as a SQL literal for filling existing rows of an ``auto_now``/
        ``auto_now_add`` field, written exactly as a regular save() would write it.

        Args:
            field: An auto_now/auto_now_add DatetimeField or TimeField.
            model: The field's model.

        Returns:
            The SQL literal.
        """
        auto_now_field = cast("DatetimeField[Any] | TimeField[Any]", field)
        return self.editor.client.dialect.literals.get_literal_sql(
            self.editor.client.dialect.types.get_db_value(auto_now_field, auto_now_field.get_auto_now_value(), model)
        )

    async def added_column_backfill_sql_literal(self, field: Field[Any], model: type[Model]) -> str:
        """``field``'s Python default as a SQL literal for the rows that exist when its column is
        added. A callable (or async) default is evaluated once, so every existing row gets that
        same value.

        Args:
            field: The field being added.
            model: The field's model.

        Returns:
            The SQL literal.
        """
        if field.default is None and self.is_auto_now_field(field):
            return self.auto_now_backfill_sql_literal(field, model)
        default_value = field.default() if callable(field.default) else field.default
        if inspect.isawaitable(default_value):
            default_value = await default_value
        return self.editor.client.dialect.literals.get_literal_sql(
            self.editor.client.dialect.types.get_db_value(field, default_value, model)
        )

    def get_added_column_backfill_plan(self, model: type[Model], field: Field[Any], db_field: str) -> SchemaChangePlan:
        """Prepare a constant assignment, or an explicit default-evaluation step."""
        table = self.editor.get_row_change_table_sql(model)
        column = self.editor.quote(db_field)
        if (
            callable(field.default)
            or inspect.isawaitable(field.default)
            or (field.default is None and self.is_auto_now_field(field))
        ):
            return SchemaChangePlan(
                (
                    ColumnDefaultBackfill(
                        model, copy(field), table, column, recovery=self.editor.get_row_recovery(model)
                    ),
                )
            )
        value = self.backfill_default_sql_literal(field, field.default, model)
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.get_fill_nulls_sql(table, column, value),
                    effects=frozenset({SchemaEffect.BACKFILL_ROWS}),
                    recovery=self.editor.get_row_recovery(model),
                ),
            )
        )

    def get_fill_nulls_sql(self, table_sql: str, column_sql: str, value_sql: str) -> str:
        """The dialect's assignment to existing NULL rows, without evaluating the value."""
        return self.editor.UPDATE_ROWS_TEMPLATE.format(
            table=table_sql, assignments=f"{column_sql} = {value_sql}", condition=f"{column_sql} IS NULL"
        )

    def get_added_column_default_plan(self, model: type[Model], field: Field[Any], db_field: str) -> SchemaChangePlan:
        """Prepare persistence of a database default; ordinary ADD COLUMN already stores it."""
        return SchemaChangePlan(())

    def get_added_column_not_null_plan(self, model: type[Model], field: Field[Any], db_field: str) -> SchemaChangePlan:
        """Prepare the final NOT NULL change; a rebuild includes its target definition."""
        column_definitions = self.editor.column_definitions
        table_column_type = column_definitions.get_table_column_type(model, field)
        sql_type = column_definitions.get_altered_column_type(table_column_type, False)
        return self.get_not_null_plan(
            model,
            field,
            self.editor.quote(db_field),
            sql_type,
            column_has_db_default=field.has_db_default(),
        )

    def get_not_null_plan(
        self,
        model: type[Model],
        field: Field[Any],
        column: str,
        sql_type: str,
        *,
        column_has_db_default: bool,
    ) -> SchemaChangePlan:
        """The ALTER making a column NOT NULL, its NULL rows already given a value.

        Args:
            model: The column's model.
            field: The column's field, as it ends up.
            column: The quoted column.
            sql_type: The column's type, as it ends up.
            column_has_db_default: Whether the column has a database default before the change.

        Returns:
            The prepared column changes with their explicit failure targets.
        """
        sql = self.editor.get_column_change_sql(
            model,
            self.editor.ALTER_FIELD_TEMPLATE,
            changes=self.editor.ALTER_FIELD_NOT_NULL_TEMPLATE.format(column=column, sql_type=sql_type),
        )
        return SchemaChangePlan((SchemaStatement(sql, recovery=self.editor.get_schema_recovery(model)),))

    def field_backfill_sql_literal(self, field: Field[Any], model: type[Model]) -> str | None:
        """The SQL literal existing rows get in a column: the field's ``default``, else its
        ``db_default``.

        Returns:
            The literal, None when the field has neither.
        """
        if field.default is not None:
            return self.backfill_default_sql_literal(field, field.default, model)
        if field.has_db_default():
            return self.editor.column_definitions.get_field_db_default_sql(field, model)
        if self.is_auto_now_field(field):
            return self.auto_now_backfill_sql_literal(field, model)
        return None

    def get_row_identity_sql(self) -> str | None:
        """The SQL naming a row of a table without a primary key, which a backfill's batches are
        picked by.

        Returns:
            The column - None when the database names no row otherwise.
        """
        return None

    def get_backfill_key_sql(self, model: type[Model], field_name: str) -> list[str]:
        """The columns a backfill's batches pick their rows by: the primary key's - every one of
        a composite key - or the database's own name of a row for a model without one.

        Args:
            model: The model backfilled.
            field_name: The field filled, for the error.

        Returns:
            The quoted columns.

        Raises:
            ConfigurationError: The model has no primary key and the database names no row otherwise.
        """
        meta = model._meta
        if meta.has_primary_key:
            return [self.editor.quote(meta.fields_db_projection[name]) for name in meta.primary_key_attribute_names]
        row_identity_sql = self.get_row_identity_sql()
        if row_identity_sql is None:
            raise ConfigurationError(
                BACKFILL_WITHOUT_ROW_IDENTITY_MESSAGE.format(
                    model=model.__name__, field=field_name, dialect=self.editor.client.dialect
                )
            )
        return [row_identity_sql]

    def get_backfill_batch_sql(
        self, qualified_table: str, quoted_column: str, quoted_key_columns: list[str], batch_size: int
    ) -> str:
        """One batch of filling a column's NULLs: sets the column to parameter 1 in up to
        ``batch_size`` rows where it is NULL and differs, NULL-safely, from parameter 2 - the same
        value. Without the second condition a NULL fill value would never end the batches.

        Args:
            qualified_table: The schema-qualified, quoted table.
            quoted_column: The quoted column to fill.
            quoted_key_columns: The quoted columns a batch's rows are picked by
                (``get_backfill_key_sql()``).
            batch_size: The most rows a batch writes.

        Returns:
            The statement.
        """
        dialect = self.editor.client.dialect
        selected_keys = ", ".join(quoted_key_columns)
        key = quoted_key_columns[0] if len(quoted_key_columns) == 1 else f"({selected_keys})"
        # The outer NULL check too: a row address unique only within a partition never picks a
        # row already filled.
        return self.editor.UPDATE_ROWS_TEMPLATE.format(
            table=qualified_table,
            assignments=f"{quoted_column} = {dialect.parameters.get_placeholder(1)}",
            condition=(
                f"{quoted_column} IS NULL AND {key} IN ("  # nosec B608
                f"SELECT {selected_keys} FROM {qualified_table} "
                f"WHERE {quoted_column} IS NULL "
                f"AND {dialect.renderers.get_distinct_from_sql(quoted_column, dialect.parameters.get_placeholder(2))}"
                f"{dialect.clauses.get_limit_offset_sql(str(batch_size), None)})"
            ),
        )

    def get_null_count_sql(self, qualified_table: str, quoted_column: str) -> str:
        """Counts a column's NULLs, as ``null_count``.

        Args:
            qualified_table: The schema-qualified, quoted table.
            quoted_column: The quoted column.

        Returns:
            The statement.
        """
        return f"SELECT COUNT(*) AS null_count FROM {qualified_table} WHERE {quoted_column} IS NULL"  # nosec B608

    def get_stored_value_rewrite_plan(
        self, model: type[Model], old_field: Field[Any], new_field: Field[Any]
    ) -> SchemaChangePlan:
        """Compile a field conversion and row identity before changing the schema."""
        conversion = copy(new_field).get_stored_value_conversion(copy(old_field))
        if conversion is None:
            return SchemaChangePlan(())
        meta = model._meta
        description = f"{model.__name__}.{new_field.model_field_name}"
        meta.raise_if_no_primary_key(f"Rewriting the stored values of {description}")
        dialect = self.editor.client.dialect
        table = self.editor.qualify_table_name(meta.db_table, meta.schema)
        column = meta.fields_db_projection[new_field.model_field_name]
        key_columns = tuple(meta.fields_db_projection[name] for name in meta.primary_key_attribute_names)
        quoted_keys = [self.editor.quote(name) for name in key_columns]
        quoted_column = self.editor.quote(column)
        select_sql = (
            f"SELECT {', '.join(quoted_keys)}, {quoted_column} FROM {table} "  # nosec B608
            f"WHERE {quoted_column} IS NOT NULL ORDER BY {', '.join(quoted_keys)}"
        )
        update_sql = self.editor.UPDATE_ROWS_TEMPLATE.format(
            table=self.editor.get_row_change_table_sql(model),
            assignments=f"{quoted_column} = {dialect.parameters.get_placeholder(1)}",
            condition=" AND ".join(
                f"{key} = {dialect.parameters.get_placeholder(position)}"
                for position, key in enumerate(quoted_keys, start=2)
            ),
        )
        return SchemaChangePlan(
            (
                StoredValueRewrite(
                    select_sql,
                    update_sql,
                    column,
                    key_columns,
                    conversion,
                    description,
                    recovery=self.editor.get_row_recovery(model),
                ),
            )
        )
