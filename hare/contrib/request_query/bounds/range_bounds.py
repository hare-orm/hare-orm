from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from hare.contrib.request_query.bounds.parameter_bounds import ParameterBounds
from hare.contrib.request_query.constants import BOUNDS_ERROR_TYPE
from hare.contrib.request_query.exceptions import InvalidRequestQuery


@dataclasses.dataclass(frozen=True, slots=True)
class RangeBounds(ParameterBounds):
    """The two values of one ``range`` parameter - ``published_at__range``.

    Args:
        parameter: The parameter.
    """

    parameter: str

    def check(self, values: Mapping[str, Any]) -> None:
        value = values.get(self.parameter)
        if value is None:
            return
        start, end = value
        if self.leaves_nothing(start, end, strict=False):
            raise InvalidRequestQuery(
                [{"loc": [self.parameter], "msg": "The range starts after it ends", "type": BOUNDS_ERROR_TYPE}]
            )
