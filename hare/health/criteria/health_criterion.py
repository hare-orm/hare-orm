from __future__ import annotations

import abc
from typing import TYPE_CHECKING

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
