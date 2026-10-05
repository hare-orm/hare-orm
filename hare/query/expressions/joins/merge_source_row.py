from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError
from hare.query.constants import MERGE_SOURCE_ALIAS, MERGE_SOURCE_KEY
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.joins.named_columns import NamedColumns
from hare.sql import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions.expression_context import ExpressionContext


class MergeSourceRow(NamedColumns):
    """The source row ``merge()`` matches - a branch reads its columns as ``merge_source__<column>``:
    ``F("merge_source__delivered")`` in the written values, ``Q(merge_source__count__gt=0)`` in a
    condition. ``F("<field>")`` reads the model's row.

    Args:
        fields_by_column: The source's columns, each with the field its values are of - None when
            not known.
    """

    def __init__(self, fields_by_column: Mapping[str, Field[Any] | None]) -> None:
        self.fields_by_column = dict(fields_by_column)

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """The source column ``path`` names.

        Raises:
            FieldError: ``path`` isn't a column of the source.
        """
        if path not in self.fields_by_column:
            columns = ", ".join(self.fields_by_column)
            raise FieldError(
                f"{MERGE_SOURCE_KEY}__<column> reads a column of merge()'s source ({columns}), got {path!r}"
            )
        return ExpressionResult(term=Table(MERGE_SOURCE_ALIAS)[path], output_field=self.fields_by_column[path])

    def __repr__(self) -> str:
        return f"MergeSourceRow({list(self.fields_by_column)!r})"
