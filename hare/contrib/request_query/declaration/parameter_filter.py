from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class ParameterFilter:
    """How a parameter a request query class declares filters.

    Attributes:
        filter_key: The ``.filter()`` key its value goes to; None for a filter method.
        method_name: The ``filter_<parameter>`` method turning its value into a condition; None for
            a plain filter.
    """

    filter_key: str | None
    method_name: str | None
