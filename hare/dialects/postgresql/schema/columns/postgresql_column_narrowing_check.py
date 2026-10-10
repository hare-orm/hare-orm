from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
from hare.dialects.postgresql.schema.constants import POSTGRESQL_HELD_VALUE_ALIAS
from hare.exceptions import UnSupportedError
from hare.fields.enums import HeldValueStep

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.containers.container_field import HeldValuePath
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit


class PostgresqlColumnNarrowingCheck(ColumnNarrowingCheck):
    """The narrowing check of PostgreSQL - the values of an array column read by ``unnest()``, which reads
    the elements of every dimension."""

    __slots__ = ()

    def get_held_value_overflow_predicate_sql(
        self, held_path: HeldValuePath, limit: NarrowingLimit, quoted_column: str
    ) -> str:
        """A WHERE predicate matching the rows holding a value beyond a narrowing limit in an array
        column, at any depth.

        Args:
            held_path: The way from the column to the values.
            limit: Their limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.

        Raises:
            UnSupportedError: The way crosses something but arrays - a map or a tuple.
        """
        if any(step != HeldValueStep.ELEMENT for step, _position in held_path):
            raise UnSupportedError(f"The {self.editor.client.dialect} dialect checks the narrowing of arrays alone")
        element_sql = POSTGRESQL_HELD_VALUE_ALIAS
        predicate_sql = self.get_narrowing_overflow_predicate_sql(limit, element_sql)
        return f"EXISTS (SELECT 1 FROM unnest({quoted_column}) AS {element_sql} WHERE {predicate_sql})"  # nosec B608
