from __future__ import annotations

import operator
from collections.abc import Callable
from functools import reduce
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.filters.field_lookup import FieldLookup

    LookupFunc = Callable[["Field[Any] | None"], FieldLookup]


class FieldMeta(type):
    @staticmethod
    def get_field_class() -> type:
        """``Field`` - asked only once it exists: this metaclass creates ``Field`` itself too."""
        # Imported here: the modules import each other.
        from hare.fields.base.field import Field

        return Field

    # TODO: Require functions to return field instances instead of this hack
    def __new__(mcs, name: str, bases: tuple[type, ...], attrs: dict[str, Any]) -> type:
        if len(bases) > 1 and bases[0] is mcs.get_field_class():
            # Instantiate class with only the 1st base class (should be Field)
            cls = type.__new__(mcs, name, (bases[0],), attrs)
            # All other base classes are our meta types, we store them in class attributes
            field_type = bases[1] if len(bases) == 2 else reduce(operator.or_, bases[1:])
            setattr(cls, "field_type", field_type)
        else:
            cls = type.__new__(mcs, name, bases, attrs)
        if ("to_python" in attrs or "from_db_value" in attrs) and "keeps_native_db_values" not in attrs:
            # A conversion of its own may change even a value the driver already returns as
            # field_type - only the class defining it can say it doesn't.
            cls.keeps_native_db_values = False  # type: ignore[attr-defined]
        return cls
