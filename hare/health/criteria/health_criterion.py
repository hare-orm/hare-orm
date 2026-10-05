from __future__ import annotations

import abc
import math
from typing import TYPE_CHECKING

from hare.classes.value_descriptions import ValueDescriptions
from hare.health.constants import POOL_DESCRIPTION, TENANT_SCHEMA_POOL_DESCRIPTION

if TYPE_CHECKING:
    from hare.health.declarations import ConnectionCheck
    from hare.instrumentation.declarations import PoolStatus


class HealthCriterion(abc.ABC):
    """A condition a health check judges a connection by - degrading or unhealthy, as the list it is
    in (``HealthCheck(degraded_when=..., unhealthy_when=...)``). A criterion of one's own subclasses
    it and gives ``check()``.
    """

    #: Whether the criterion reads the measured waits for a connection - the health check then needs
    #: ``PoolMetrics`` enabled.
    requires_pool_metrics: bool = False

    @abc.abstractmethod
    def check(self, connection_check: ConnectionCheck) -> str | None:
        """Whether the criterion holds for a connection.

        Args:
            connection_check: What the health check saw of the connection.

        Returns:
            Why it holds, None when it doesn't.
        """

    @staticmethod
    def describe_pool(status: PoolStatus) -> str:
        """How a reason names a pool.

        Args:
            status: The pool.

        Returns:
            The name.
        """
        if status.schema is not None:
            return TENANT_SCHEMA_POOL_DESCRIPTION.format(schema=status.schema)
        return POOL_DESCRIPTION.format(role=status.role.value)

    @staticmethod
    def get_checked_count(name: str, value: int) -> int:
        """A count a criterion takes - a whole number of at least 1.

        Args:
            name: The argument, for the error.
            value: The value.

        Returns:
            The value.

        Raises:
            ValueError: It is below 1.
        """
        if value < 1:
            raise ValueError(f"{name} must be at least 1, got {ValueDescriptions.get_shown_text(value)}")
        return value

    @staticmethod
    def get_checked_amount(name: str, value: float, maximum: float) -> float:
        """An amount a criterion takes - a finite number above 0, at most ``maximum``.

        Args:
            name: The argument, for the error.
            value: The value.
            maximum: The largest value taken.

        Returns:
            The value.

        Raises:
            ValueError: It is out of range.
        """
        if not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(
                f"{name} must be above 0 and at most {maximum:g}, got {ValueDescriptions.get_shown_text(value)}"
            )
        return float(value)
