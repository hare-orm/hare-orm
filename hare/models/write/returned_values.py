from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model

TInstance = TypeVar("TInstance", bound="Model")


class ReturnedValues:
    """The values a write's ``RETURNING`` rows hold - the columns the database computed - set on
    the written instances."""

    @staticmethod
    def apply_row(
        model: type[Model],
        types: TypeRegistry,
        obj: Model,
        row: Any,
        columns: Iterable[str] | None = None,
    ) -> None:
        """Sets on an obj every field a ``RETURNING`` row holds a column of.

        Args:
            model: The written model.
            types: The connection's type registry.
            obj: The obj whose row was written.
            row: The row, by column name.
            columns: Only these columns of the row - every one by default.
        """
        meta = model._meta
        field_names_by_column = meta.fields_db_projection_reverse
        fields_map = meta.fields_map
        for column in row.keys() if columns is None else columns:
            field_name = field_names_by_column.get(column)
            if field_name is not None:
                setattr(obj, field_name, types.get_python_value(fields_map[field_name], row[column]))

    @staticmethod
    def match_rows(
        model: type[Model],
        types: TypeRegistry,
        instances: Iterable[TInstance],
        rows: Sequence[Any],
        field_names: Sequence[str],
    ) -> Iterator[tuple[TInstance, dict[str, Any]]]:
        """Pairs each ``RETURNING`` row with the instance holding the row's values of the given
        fields - the first such instance; a row no instance holds is skipped.

        Args:
            model: The written model.
            types: The connection's type registry.
            instances: The written instances.
            rows: The rows.
            field_names: The fields telling the rows apart - the primary key, or a conflict target.

        Yields:
            Each instance with its row, by column name.
        """
        meta = model._meta
        fields = [meta.fields_map[field_name] for field_name in field_names]
        columns = [meta.fields_db_projection[field_name] for field_name in field_names]
        instances_by_key: dict[tuple[Any, ...], TInstance] = {}
        for instance in instances:
            instances_by_key.setdefault(tuple(getattr(instance, field_name) for field_name in field_names), instance)
        for raw_row in rows:
            # A sqlite3.Row has no .items()/.get() - read as a dict like a PostgreSQL row.
            row = dict(raw_row)
            key = tuple(
                types.get_python_value(field, row.get(column)) for field, column in zip(fields, columns, strict=True)
            )
            if key in instances_by_key:
                yield instances_by_key.pop(key), row
