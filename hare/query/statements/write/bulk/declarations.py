from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


@dataclasses.dataclass(frozen=True, slots=True)
class BulkUpdateStatementLayout:
    """What every ``UPDATE ... FROM (VALUES ...)`` statement of one ``bulk_update()`` shares - each
    batch of objects only adds its rows.

    Attributes:
        table: The model's table.
        base_context: The dialect's base SQL context.
        filter_criterion: The queryset's own filter, None for none.
        assignments: The SET of each written column - the column and its new value's SQL.
        matches: The WHERE equalities matching a VALUES row to a table row.
        returned: The RETURNING values.
        values_columns_sql: The column list of the VALUES table.
        values_alias_sql: The quoted alias of the VALUES table.
        fields: The written column fields.
        field_cast_types: The cast type of each written field, None for none.
        field_placeholder_templates: The placeholder template of each written field.
        pk_column_count: The columns of the primary key.
        pk_field_objects: The fields of the primary key.
        optimistic_lock_field: The ``Meta.optimistic_lock_field``, None without one.
        optimistic_lock_field_object: Its field, None without one.
        key_cast_types: The cast type of each key column and of the old version.
        key_placeholder_templates: The placeholder template of each key column and of the old version.
        row_fields: The fields of a VALUES row - the key, the old version, the written fields.
        row_placeholder_templates: The placeholder template of each of them.
        serializes_whole_rows: Whether the field codecs serialize a whole row - when the key
            columns write no ``auto_now`` stamp.
        batch_size: How many objects one statement writes.
    """

    table: Table
    base_context: SqlContext
    filter_criterion: Term | None
    assignments: list[tuple[str, str]]
    matches: list[tuple[str, str]]
    returned: list[ReturnedValue]
    values_columns_sql: str
    values_alias_sql: str
    fields: list[str]
    field_cast_types: list[str | None]
    field_placeholder_templates: list[str]
    pk_column_count: int
    pk_field_objects: list[Field[Any]]
    optimistic_lock_field: str | None
    optimistic_lock_field_object: Field[Any] | None
    key_cast_types: list[str | None]
    key_placeholder_templates: list[str]
    row_fields: list[str]
    row_placeholder_templates: tuple[str, ...]
    serializes_whole_rows: bool
    batch_size: int | None
