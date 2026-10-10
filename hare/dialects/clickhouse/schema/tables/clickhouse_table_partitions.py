from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.tables.table_partitions import TablePartitions
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_ALTERABLE_TABLE_OPTIONS,
    CLICKHOUSE_CREATION_ONLY_TABLE_SETTINGS,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.models import Model


class ClickhouseTablePartitions(TablePartitions):
    """A change of a ClickHouse table's options - by ``ALTER TABLE`` where the server changes the
    option in place (the table's and its columns' time to live, its settings, the compression of its
    columns, its projections), by remaking the table for its engine and its keys."""

    __slots__ = ()

    async def alter_table_options(
        self, model: type[Model], old_options: TableOptions | None, new_options: TableOptions | None
    ) -> None:
        old = old_options if isinstance(old_options, ClickhouseTableOptions) else ClickhouseTableOptions()
        new = new_options if isinstance(new_options, ClickhouseTableOptions) else ClickhouseTableOptions()
        changed_options = {
            option.name for option in dataclasses.fields(new) if getattr(old, option.name) != getattr(new, option.name)
        }
        old_settings, new_settings = dict(old.get_table_settings()), dict(new.get_table_settings())
        changed_settings = {
            name for name in {*old_settings, *new_settings} if old_settings.get(name) != new_settings.get(name)
        }
        if (
            changed_options - CLICKHOUSE_ALTERABLE_TABLE_OPTIONS
            or changed_settings & CLICKHOUSE_CREATION_ONLY_TABLE_SETTINGS
        ):
            await self.editor.table_rebuild.remake_table(model)
            return
        for statement in self.get_alter_sqls(model, old, new):
            await self.editor.run_sql(statement)

    def get_alter_sqls(
        self, model: type[Model], old: ClickhouseTableOptions, new: ClickhouseTableOptions
    ) -> list[str]:
        """The statements changing a table from its old options to its new ones in place.

        Args:
            model: The model rendered with its new options.
            old: The table's options.
            new: The options it gets.

        Returns:
            The statements.
        """
        quote = self.editor.quote
        meta = model._meta
        table_sql = self.editor.qualify_table_name(meta.db_table, meta.schema)
        changes: list[str] = []
        if old.ttl != new.ttl:
            changes.append("REMOVE TTL" if new.ttl is None else f"MODIFY TTL {new.ttl.sql}")
        old_settings, new_settings = dict(old.get_table_settings()), dict(new.get_table_settings())
        modified_settings = [
            f"{name} = {new.get_setting_sql(value)}"
            for name, value in new_settings.items()
            if old_settings.get(name) != value
        ]
        projection_settings_sql = new.get_projection_settings_sql(self.editor.client.features)
        if projection_settings_sql and not old.projections:
            modified_settings.append(projection_settings_sql)
        if modified_settings:
            changes.append(f"MODIFY SETTING {', '.join(modified_settings)}")
        reset_settings = [name for name in old_settings if name not in new_settings]
        if reset_settings:
            changes.append(f"RESET SETTING {', '.join(reset_settings)}")
        changes.extend(
            self.get_column_option_changes(model, dict(old.column_codecs), dict(new.column_codecs), "CODEC")
        )
        changes.extend(self.get_column_option_changes(model, dict(old.column_ttls), dict(new.column_ttls), "TTL"))
        old_projections = {projection.name: projection for projection in old.projections}
        new_projections = {projection.name: projection for projection in new.projections}
        changes.extend(
            f"DROP PROJECTION {quote(name)}"
            for name, projection in old_projections.items()
            if new_projections.get(name) != projection
        )
        added_projections = [
            projection for name, projection in new_projections.items() if old_projections.get(name) != projection
        ]
        changes.extend(f"ADD PROJECTION {projection.get_definition_sql(quote)}" for projection in added_projections)
        # A projection added to a table holding rows is built for them by a mutation of its own.
        changes.extend(f"MATERIALIZE PROJECTION {quote(projection.name)}" for projection in added_projections)
        return [f"ALTER TABLE {table_sql} {change}" for change in changes]

    def get_column_option_changes(
        self, model: type[Model], old_values: dict[str, Any], new_values: dict[str, Any], clause: str
    ) -> list[str]:
        """The changes of one option of the table's columns - set where it is new or another, removed
        where it is gone.

        Args:
            model: The model.
            old_values: The option of each field naming one.
            new_values: The option each field gets.
            clause: The option's clause - ``CODEC`` or ``TTL``.

        Returns:
            The ``MODIFY COLUMN`` changes.
        """
        quote = self.editor.quote
        meta = model._meta
        fields_db_projection = meta.fields_db_projection
        column_definitions = self.editor.column_definitions
        changes = []
        for field_name, value in new_values.items():
            if old_values.get(field_name) == value:
                continue
            field = meta.fields_map[field_name]
            # With its type: a clause right after the column's name is read as its type.
            column_type = column_definitions.get_altered_column_type(
                column_definitions.get_table_column_type(model, field), field.null
            )
            value_sql = f"CODEC({value})" if clause == "CODEC" else f"TTL {value.sql}"
            changes.append(f"MODIFY COLUMN {quote(fields_db_projection[field_name])} {column_type} {value_sql}")
        changes.extend(
            f"MODIFY COLUMN {quote(fields_db_projection[field_name])} REMOVE {clause}"
            for field_name in old_values
            if field_name not in new_values and field_name in fields_db_projection
        )
        return changes
