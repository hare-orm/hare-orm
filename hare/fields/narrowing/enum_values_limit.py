from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


@dataclass(frozen=True)
class EnumValuesLimit(NarrowingLimit):
    """A value is one of an enum's stored values - a member dropped from it may not be stored."""

    values: frozenset[Any]

    @classmethod
    def get_for_dropped_members(
        cls, enum_type: type[Enum], old_enum_type: type[Enum], stored_value: Callable[[Any], Any]
    ) -> EnumValuesLimit | None:
        """The limit of an enum field whose enum lost members the old field's enum had.

        Args:
            enum_type: The new enum.
            old_enum_type: The old enum.
            stored_value: Turns a member's value into the value the column stores.

        Returns:
            The limit, None when no member was dropped.
        """
        stored_values = frozenset(stored_value(member.value) for member in enum_type)
        if {stored_value(member.value) for member in old_enum_type} <= stored_values:
            return None
        return cls(stored_values)

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        return check.get_enum_overflow_predicate_sql(self, quoted_column)
