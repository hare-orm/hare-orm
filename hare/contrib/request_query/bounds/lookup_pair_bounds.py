from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from hare.contrib.request_query.bounds.constants import BOUNDS_ERROR_TYPE
from hare.contrib.request_query.bounds.parameter_bounds import ParameterBounds
from hare.contrib.request_query.exceptions import InvalidRequestQuery


@dataclasses.dataclass(frozen=True, slots=True)
class LookupPairBounds(ParameterBounds):
    """A lower and an upper bound of one field given by two parameters -
    ``published_at__gte`` and ``published_at__lte``.

    Args:
        lower_parameter: The parameter of the lower bound (``gt``/``gte``).
        upper_parameter: The parameter of the upper bound (``lt``/``lte``).
        strict: Whether either bound excludes its own value.
    """

    lower_parameter: str
    upper_parameter: str
    strict: bool

    def check(self, values: Mapping[str, Any]) -> None:
        lower = values.get(self.lower_parameter)
        upper = values.get(self.upper_parameter)
        if lower is None or upper is None or not self.leaves_nothing(lower, upper, self.strict):
            return
        raise InvalidRequestQuery(
            [
                {
                    "loc": [self.upper_parameter],
                    "msg": f"Leaves no value together with {self.lower_parameter}",
                    "type": BOUNDS_ERROR_TYPE,
                }
            ]
        )
