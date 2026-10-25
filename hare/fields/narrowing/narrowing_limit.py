from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


class NarrowingLimit(abc.ABC):
    """What a narrowed column holds at most - the rows beyond it are found by the check of the dialect's
    schema editor."""

    __slots__ = ()

    @abc.abstractmethod
    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose value is beyond the limit.

        Args:
            check: The narrowing check of the schema editor.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
