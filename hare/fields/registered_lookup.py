from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.enums import LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.field_lookup import FieldLookup


@dataclass(frozen=True, slots=True)
class RegisteredLookup:
    """A custom ``__<name>`` lookup of a field class and what its filter value looks like.

    Attributes:
        builder: Builds the lookup for one field - ``(field) -> FieldLookup``; ``field`` is None
            for a value with no field (a lookup registered on ``Field`` itself).
        value_shape: Whether the filter value is one value, a list or a two-item range.
        value_type: The type of the value (of each item of a list or range), None for the field's
            own type.
        dialects: The names of the dialects the lookup runs on, None for every dialect.
        required_extension: The database extension the lookup needs, None for none.
    """

    builder: Callable[[Any], FieldLookup]
    value_shape: LookupValueShape = LookupValueShape.VALUE
    value_type: Any = None
    dialects: frozenset[str] | None = None
    required_extension: str | None = None
