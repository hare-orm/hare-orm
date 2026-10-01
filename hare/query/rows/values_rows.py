from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.enums import RowShape
from hare.query.rows.hydrate_accelerator import HydrateAccelerator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.rows.value_field import ValueField

#: A selected column's alias (or its position in a driver row) with the function decoding its
#: value, None when the raw value is used as-is.
ColumnConverter = tuple[Any, Callable[[Any], Any] | None]


class ValuesRows:
    """How a row of the values ``.values()``/``.values_list()`` select is read - one class per
    shape of a row. A composite key selects one column per key field, returned combined as one
    tuple (None when every column is NULL)."""

    #: The shape the plan key of the query holds - shapes selecting the same columns share one.
    plan_shape: ClassVar[RowShape]
    #: Whether a column is selected under its output key rather than its position - a row is then
    #: read by key, and an annotation the row doesn't name is left out of the SELECT list.
    keyed_by_output_name: ClassVar[bool] = False

    def __init__(self, output_names: tuple[str, ...], output_column_aliases: Sequence[tuple[str, ...]]) -> None:
        """
        Args:
            output_names: The names of a row's values as returned - the dict keys, the selected
                names of a tuple.
            output_column_aliases: Per returned value, the aliases of its columns.
        """
        self.output_names = output_names
        self.output_column_aliases = output_column_aliases
        self.has_composite_outputs = any(len(column_aliases) > 1 for column_aliases in output_column_aliases)

    @classmethod
    def get_column_aliases(cls, output_name: str, first_column_index: int, column_count: int) -> tuple[str, ...]:
        """The aliases the columns of one returned value are selected under.

        Args:
            output_name: The value's output name.
            first_column_index: How many columns are selected before it.
            column_count: How many columns the value selects - more than one for a composite key.

        Returns:
            The aliases.
        """
        raise NotImplementedError()  # pragma: nocoverage

    @staticmethod
    def get_composite_key_value(component_values: Sequence[Any]) -> tuple[Any, ...] | None:
        """A composite key read back from its columns - None when every column is NULL (a null
        foreign key, or no related row).

        Args:
            component_values: The key's column values, in key order.

        Returns:
            The key tuple, or None.
        """
        if all(component_value is None for component_value in component_values):
            return None
        return tuple(component_values)

    async def fetch(
        self,
        db: DatabaseClient,
        sql: str,
        params: list[Any],
        column_converters: list[ColumnConverter],
        model: type[Model] | None = None,
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        """Runs a statement selecting the columns and builds the rows.

        Args:
            db: The connection.
            sql: The SQL.
            params: Its parameters.
            column_converters: Each column's alias and decoder.
            model: The queried model.
            value_fields: What each column is read as - the rows are then built in one
                ``rust.native.rows`` call where the driver's rows are read by position.

        Returns:
            The rows.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def read_with_accelerator(
        self, reader: Any, rows: list[Any], column_converters: list[ColumnConverter]
    ) -> list[Any]:
        """The rows of positional driver rows whose columns are the selected ones in order, built by
        a ``rust.native.rows.ValuesReader``.

        Args:
            reader: The reader of the selected columns.
            rows: The driver rows.
            column_converters: Each column's alias and decoder.

        Returns:
            The rows.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def convert_batch(
        self,
        db: DatabaseClient,
        rows: list[Any],
        column_converters: list[ColumnConverter],
        model: type[Model] | None = None,
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        """The rows of a batch of driver rows - in one ``rust.native.rows`` call when the value
        fields are known and the rows are read by position, else one at a time.

        Args:
            db: The connection the rows were read on.
            rows: The driver rows, at least one.
            column_converters: Each column's alias and decoder.
            model: The queried model.
            value_fields: What each column is read as.

        Returns:
            The rows.
        """
        if (
            model is not None
            and value_fields is not None
            and HydrateAccelerator.module is not None
            and db.features.supports_positional_rows
            and not self.has_composite_outputs
            and list(rows[0].keys()) == [alias for alias, _func in column_converters]
        ):
            return HydrateAccelerator.run_or_fall_back(
                lambda: self.read_with_accelerator(
                    HydrateAccelerator.get_values_reader(model, value_fields, db), rows, column_converters
                ),
                lambda: [self.convert(dict(row), column_converters) for row in rows],
            )
        return [self.convert(dict(row), column_converters) for row in rows]

    def convert(self, row: dict[str, Any], column_converters: list[ColumnConverter]) -> Any:
        """Builds one row from a fetched row.

        Args:
            row: The fetched row, by column alias.
            column_converters: Each column's alias and decoder.

        Returns:
            The row.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def get_reader(self, position: int, alias: str) -> Callable[[Any], Any]:
        """Reads the value of a selected column off one row.

        Args:
            position: The column's position among the selected ones.
            alias: Its alias.

        Returns:
            The reader.
        """
        raise NotImplementedError()  # pragma: nocoverage
