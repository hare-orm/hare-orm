from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.lookup_info.lookup_info import LookupInfo


@dataclasses.dataclass(frozen=True, slots=True)
class FilterDeclaration:
    """One parameter a request query filters by.

    Attributes:
        parameter: The parameter's name.
        filter_key: The ``.filter()`` key its value goes to; None for a filter method.
        lookup_info: The ORM's description of ``filter_key``; None for a filter method.
        method_name: The ``filter_<parameter>`` method turning the value into a condition; None for
            a plain filter.
    """

    parameter: str
    filter_key: str | None
    lookup_info: LookupInfo | None
    method_name: str | None
