from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from hare.query.constants import COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR
from hare.query.enums import RowShape
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.values_rows import ColumnConverter, ValuesRows

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.rows.value_field import ValueField


class DictRows(ValuesRows):
    """``.values()``: a dict by output key."""

    plan_shape = RowShape.DICT
    keyed_by_output_name = True

    @classmethod
    def get_column_aliases(cls, output_name: str, first_column_index: int, column_count: int) -> tuple[str, ...]:
        if column_count == 1:
            return (output_name,)
        return tuple(f"{output_name}{COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR}{index}" for index in range(column_count))

    def get_row(self, column_row: dict[str, Any]) -> dict[str, Any]:
        """A returned dict from its selected columns - a composite key's columns combined into one
        tuple under its output key.

        Args:
            column_row: The selected columns by alias.

        Returns:
            The row.
        """
        if not self.has_composite_outputs:
            return column_row
        return {
            output_name: column_row[column_aliases[0]]
            if len(column_aliases) == 1
            else self.get_composite_key_value([column_row[alias] for alias in column_aliases])
            for output_name, column_aliases in zip(self.output_names, self.output_column_aliases, strict=True)
        }

    async def fetch(
        self,
        db: DatabaseClient,
        sql: str,
        params: list[Any],
        column_converters: list[ColumnConverter],
        model: type[Model] | None = None,
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        if (
            model is not None
            and value_fields is not None
            and HydrateAccelerator.module is not None
            and db.features.supports_positional_rows
            and not self.has_composite_outputs
        ):
            _, fetched_rows = await db.execute(sql, params, returns_rows=True)
            if not fetched_rows:
                return []
            positional_rows = fetched_rows if type(fetched_rows) is list else list(fetched_rows)
            return self.convert_batch(db, positional_rows, column_converters, model, value_fields)
        rows = await db.execute_dicts(sql, params)
        converted_columns = [(alias, func) for alias, func in column_converters if func is not None]
        if converted_columns:
            for row in rows:
                for alias, func in converted_columns:
                    row[alias] = func(row[alias])
        if self.has_composite_outputs:
            return [self.get_row(row) for row in rows]
        return rows

    def read_with_accelerator(
        self, reader: Any, rows: list[Any], column_converters: list[ColumnConverter]
    ) -> list[Any]:
        return cast("list[Any]", reader.read_dicts(rows, [alias for alias, _func in column_converters]))

    def convert(self, row: dict[str, Any], column_converters: list[ColumnConverter]) -> Any:
        return self.get_row(
            {alias: row[alias] if func is None else func(row[alias]) for alias, func in column_converters}
        )

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return lambda row: row[alias]
