from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.query.execution.write_statements import BoundWriteValue

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field


class PreparedWrite:
    """Field assignments bound for a single write, shared by its SQL compilation paths.

    Keys identify a declared assignment and, for a composite relation, its component.
    This is execution state, not a model cache: a later execution binds a fresh set of
    values. SQL builders and cached plans consume the same converted assignments.
    """

    def __init__(
        self,
        dialect: Dialect,
        assignments: Iterable[tuple[str, int, Field[Any], Any]],
        automatic_values: Iterable[tuple[str, Field[Any], Any]],
    ) -> None:
        self.values = {
            (name, index): BoundWriteValue(field, value, dialect.types.get_db_value(field, value, None))
            for name, index, field, value in assignments
        }
        self.automatic_values = tuple(
            (name, BoundWriteValue(field, value, dialect.types.get_db_value(field, value, None)))
            for name, field, value in automatic_values
        )
