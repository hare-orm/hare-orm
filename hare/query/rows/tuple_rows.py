from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from hare.query.enums import RowShape
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.query.rows.values_rows import ColumnConverter, ValuesRows

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.rows.value_field import ValueField


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
        db: DatabaseClient,
        sql: str,
        params: list[Any],
        column_converters: list[ColumnConverter],
        model: type[Model] | None = None,
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        _, fetched_rows = await db.execute(sql, params, returns_rows=True)
        # Driver rows - sqlite3.Row, asyncpg.Record, rust.native.pg's PgRow - read by name or position.
        result = cast("list[Any]", fetched_rows)
        if not result:
            return []
        column_keys = self.get_column_keys(db, result[0], column_converters)
        if (
            model is not None
            and value_fields is not None
            and HydrateAccelerator.module is not None
            and not self.has_composite_outputs
            and isinstance(column_keys[0][0], int)
        ):
            rows = result if type(result) is list else list(result)
            return HydrateAccelerator.run_or_fall_back(
                lambda: self.read_with_accelerator(
                    HydrateAccelerator.get_values_reader(model, value_fields, db), rows, column_converters
                ),
                lambda: self.get_rows(rows, column_keys),
            )
        return self.get_rows(result, column_keys)

    def read_with_accelerator(
        self, reader: Any, rows: list[Any], column_converters: list[ColumnConverter]
    ) -> list[Any]:
        return cast("list[Any]", reader.read_tuples(rows))

    @staticmethod
    def get_column_keys(
        db: DatabaseClient, first_row: Any, column_converters: list[ColumnConverter]
    ) -> list[ColumnConverter]:
        """The key each column is read from a driver row by: a row whose columns are exactly the
        selected aliases, in order, is read by position - cheaper than by name on every driver;
        any other row is read by name.

        Args:
            db: The connection.
            first_row: A fetched row.
            column_converters: Each column's alias and decoder.

        Returns:
            Each column's key and decoder.
        """
        if db.features.supports_positional_rows and list(first_row.keys()) == [
            alias for alias, _func in column_converters
        ]:
            return [(position, func) for position, (_alias, func) in enumerate(column_converters)]
        return column_converters

    def get_rows(self, result: list[Any], column_keys: list[ColumnConverter]) -> list[Any]:
        """The rows of fetched driver rows.

        Args:
            result: The driver rows.
            column_keys: Each column's key in a driver row and its decoder.

        Returns:
            The rows.
        """
        rows = [
            tuple(entry[key] if func is None else func(entry[key]) for key, func in column_keys) for entry in result
        ]
        if self.has_composite_outputs:
            return [self.get_row(self.get_values(values)) for values in rows]
        return rows

    def convert(self, row: dict[str, Any], column_converters: list[ColumnConverter]) -> Any:
        return self.get_row(
            self.get_values(
                tuple(row[alias] if func is None else func(row[alias]) for alias, func in column_converters)
            )
        )

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        return lambda row: row[position]
