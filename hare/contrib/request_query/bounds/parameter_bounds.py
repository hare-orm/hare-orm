from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import Any


class ParameterBounds(abc.ABC):
    """A check of the values a request gives the parameters bounding one field."""

    __slots__ = ()

    @abc.abstractmethod
    def check(self, values: Mapping[str, Any]) -> None:
        """Checks the request's values.

        Args:
            values: The request query's values by parameter.

        Raises:
            InvalidRequestQuery: The values leave no value between the bounds.
        """

    @staticmethod
    def leaves_nothing(lower: Any, upper: Any, strict: bool) -> bool:
        """Whether no value lies between two bounds.

        Args:
            lower: The lower bound.
            upper: The upper bound.
            strict: Whether a bound excludes its own value (``gt``, ``lt``).

        Returns:
            True when the lower bound is above the upper one, or equal to it with a strict bound;
            False for values that don't compare (an aware and a naive datetime) - the database
            decides.
        """
        try:
            return bool(lower > upper or (strict and lower == upper))
        except TypeError:
            return False
