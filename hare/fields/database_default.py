from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.lookups.field_lookup import FieldLookup

    LookupFunction = Callable[["Field[Any] | None"], FieldLookup]
    from hare.fields.field import Field


class DatabaseDefault:
    """The value of a field left to its ``db_default``: a single INSERT leaves its column out; a bulk
    INSERT leaves out a column every object leaves to the database, and a column only some objects
    leave raises ``QueryError``.
    """

    def __init__(self, field: Field[Any]) -> None:
        self.field = field

    def __repr__(self) -> str:
        return f"DatabaseDefault({self.field.model_field_name!r})"

    def __str__(self) -> str:
        return "<DB_DEFAULT>"

    def __bool__(self) -> bool:
        """False - no value is set yet. ``isinstance(value, DatabaseDefault)`` tells it from other
        false values.
        """
        return False
