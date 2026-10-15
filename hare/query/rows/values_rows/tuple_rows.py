from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.query.enums import RowShape
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.values_rows.values_rows import ColumnConverter, ValuesRows

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.rows.values_rows.value_field import ValueField


class TupleRows(ValuesRows):
    """``.values_list()``: a tuple of the selected values."""

    plan_shape = RowShape.TUPLE

    @classmethod
    def get_column_aliases(cls, output_name: str, first_column_index: int, column_count: int) -> tuple[str, ...]:
        return tuple(str(first_column_index + index) for index in range(column_count))

    def get_values(self, column_values: tuple[Any, ...]) -> tuple[Any, ...]:
        """The returned values from the selected column values - a composite key's columns
        combined into one tuple.

        Args:
            column_values: The values of the selected columns, in selection order.

        Returns:
            One value per output name.
        """
        if not self.has_composite_outputs:
            return column_values
        values = []
        position = 0
        for column_aliases in self.output_column_aliases:
            next_position = position + len(column_aliases)
            values.append(
                column_values[position]
                if len(column_aliases) == 1
                else self.get_composite_key_value(column_values[position:next_position])
            )
            position = next_position
        return tuple(values)

    def get_row(self, values: tuple[Any, ...]) -> Any:
        """The row as returned, from its values."""
        return values

    async def fetch(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        column_converters: list[ColumnConverter],
        model: type[Model] | None = None,
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        # Driver rows read by position where the driver can - plain tuples, sqlite3.Row,
        # asyncpg.Record, rust.native.pg's PgResult of rows - else by name.
        by_position = connection.features.supports_positional_rows
        result = await connection.execute(sql, parameters, returns_rows=True, rows_by_position=by_position)
        rows = result.rows
        if not rows:
            return []
        if not by_position:
            return self.get_rows(rows, column_converters)
        column_names = result.column_names
        aliases = [alias for alias, _converter in column_converters]
        if list(column_names) != aliases:
            # More columns than the selected ones, or in another order - each read at its position.
            return self.get_rows(
                rows,
                [(column_names.index(alias), converter) for alias, converter in column_converters],
            )
        column_keys = [(position, converter) for position, (_alias, converter) in enumerate(column_converters)]
        if (
            model is not None
            and value_fields is not None
            and HydrateAccelerator.module is not None
            and not self.has_composite_outputs
        ):
            return HydrateAccelerator.run_or_fall_back(
                lambda: self.read_with_accelerator(
                    HydrateAccelerator.get_values_reader(model, value_fields, connection), rows, column_converters
                ),
                lambda: self.get_rows(rows, column_keys),
            )
        return self.get_rows(rows, column_keys)

    def read_with_accelerator(
        self, reader: Any, rows: Sequence[Any], column_converters: list[ColumnConverter]
    ) -> list[Any]:
        return cast("list[Any]", reader.read_tuples(rows))

    def get_rows(self, result: Sequence[Any], column_keys: list[ColumnConverter]) -> list[Any]:
        """The rows of fetched driver rows.

        Args:
            result: The driver rows.
            column_keys: Each column's key in a driver row and its decoder.

        Returns:
            The rows.
        """
        rows = [
            tuple(entry[key] if converter is None else converter(entry[key]) for key, converter in column_keys)
            for entry in result
        ]
        if self.has_composite_outputs:
            return [self.get_row(self.get_values(values)) for values in rows]
        return rows

    def convert(self, row: dict[str, Any], column_converters: list[ColumnConverter]) -> Any:
        return self.get_row(
            self.get_values(
                tuple(
                    row[alias] if converter is None else converter(row[alias])
                    for alias, converter in column_converters
                )
            )
        )

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return lambda row: row[position]
