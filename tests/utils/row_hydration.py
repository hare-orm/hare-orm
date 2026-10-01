"""Reads a model query's rows the way hare does - for tests comparing the row readers."""

from typing import Any

from hare.query.queryset import QuerySet
from hare.query.rows.model_columns import ModelColumns
from hare.utils import Timezone


class RowHydration:
    """The decode plan of a model query and the pure-Python reading of rows by it."""

    @staticmethod
    def get_decode_plan(queryset: QuerySet[Any]) -> tuple[tuple[Any, ...] | None, bool]:
        """The decode plan a model query reads its rows by.

        Args:
            queryset: The query.

        Returns:
            The decode plan (None where rows are read by name) and whether it is partial.
        """
        compiler = queryset._get_model_rows_query()
        compiler._make_query()
        return compiler._decode_plan, compiler._decode_plan_is_partial

    @staticmethod
    def hydrate_in_python(model: type[Any], row: Any, decode_plan: tuple[Any, ...], is_partial: bool) -> Any:
        """Builds the instance of one positional row through the compiled Python function.

        Args:
            model: The model.
            row: The row, read by position.
            decode_plan: The decode plan of its columns.
            is_partial: Whether only some of the model's columns are selected.

        Returns:
            The instance.
        """
        columns = ModelColumns(model, decode_plan, tuple(range(len(decode_plan))), is_partial)
        use_tz = Timezone.get_use_tz()
        tz_ctx = (use_tz, Timezone.default() if use_tz else None)
        return columns.get_hydrate_function()(row, tz_ctx, None)
