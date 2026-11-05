"""Query layouts and a field-level reference for model hydration contracts."""

from types import SimpleNamespace
from typing import Any

from hare.query.queryset import QuerySet
from tests.utils.model_rows_queries import get_model_rows_query


class RowHydration:
    """Reference values obtained without the production model row constructor."""

    @staticmethod
    def get_decode_plan(queryset: QuerySet[Any]) -> tuple[tuple[Any, ...] | None, bool]:
        """The decode plan a model query reads its rows by.

        Args:
            queryset: The query.

        Returns:
            The decode plan (None where rows are read by name) and whether it is partial.
        """
        compiler = get_model_rows_query(queryset)
        compiler._make_query()
        return compiler._decode_plan, compiler._decode_plan_is_partial

    @staticmethod
    def hydrate_in_python(model: type[Any], row: Any, decode_plan: tuple[Any, ...]) -> Any:
        """Read field values through the type registry, independently of the layout's bound readers.

        Args:
            model: The model.
            row: The row, read by position.
            decode_plan: The decode plan of its columns.

        Returns:
            A namespace of expected field values, without model state or defaults.
        """
        types = model.get_connection().dialect.types
        return SimpleNamespace(
            **{
                name: types.get_python_value(field, value)
                for (name, field, _reader), value in zip(decode_plan, row, strict=True)
            }
        )
