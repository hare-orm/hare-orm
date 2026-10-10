from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.filters.lookups.field_lookup import FieldLookup

    LookupFunction = Callable[["Field[Any] | None"], FieldLookup]


class FieldMeta(type):
    """The metaclass of fields: a field class defining a conversion of its own reads no value as the driver
    returns it."""

    def __new__(mcs, name: str, bases: tuple[type, ...], attributes: dict[str, Any]) -> type:
        cls = type.__new__(mcs, name, bases, attributes)
        if ("to_python" in attributes or "from_db_value" in attributes) and "keeps_native_db_values" not in attributes:
            # A conversion of its own may change even a value the driver already returns as
            # field_type - only the class defining it can say it doesn't.
            cls.keeps_native_db_values = False  # type: ignore[attr-defined]
        return cls
