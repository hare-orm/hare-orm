from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.enums import NarrowedValueSource
from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


@dataclass(frozen=True)
class TextLengthLimit(NarrowingLimit):
    """A value's text takes no more than ``max_length`` characters."""

    max_length: int
    source: NarrowedValueSource

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        return check.get_text_overflow_predicate_sql(self, quoted_column)
