from __future__ import annotations

from http import HTTPStatus
from typing import Any

from hare.contrib.frameworks.constants import (
    DEFAULT_LIVENESS_PATH,
    DEFAULT_READINESS_PATH,
    LIVENESS_ANSWER,
    MAX_HEALTH_STATUS_CODE,
    MIN_HEALTH_STATUS_CODE,
    UNHEALTHY_STATUS_CODE,
)
from hare.health.enums import HealthStatus
from hare.health.health_check import HealthCheck


class HealthRoutes:
    """The health routes a framework integration adds (``health_routes=``) - the same answers for
    every framework:

    - readiness (``/health/ready``): the connections pinged and judged by the health check - 200 for
      healthy, ``degraded_status_code`` for degraded, 503 for unhealthy;
    - liveness (``/health/live``): 200 while the process answers - no database touched, so a database
      outage never restarts the application.

    Neither route runs in the request's transaction or appears in the OpenAPI schema.

    Args:
        health_check: The health check of the readiness route.
        readiness_path: The readiness route's path.
        liveness_path: The liveness route's path.
        degraded_status_code: The readiness status of a degraded application - 200 by default: it
            still takes traffic.
        include_details: Whether the readiness answer carries each connection's latency, reasons and
            pools - else only the statuses, for an answer anyone may read.

    Raises:
        TypeError: An argument is of the wrong type.
        ValueError: A path doesn't start with ``/``, the paths are equal, or the status is outside
            200-599.
    """

    def __init__(
        self,
        health_check: HealthCheck,
        *,
        readiness_path: str = DEFAULT_READINESS_PATH,
        liveness_path: str = DEFAULT_LIVENESS_PATH,
        degraded_status_code: int = HTTPStatus.OK.value,
        include_details: bool = False,
    ) -> None:
        if not isinstance(health_check, HealthCheck):
            raise TypeError(f"health_check must be a HealthCheck, got {type(health_check).__name__}")
        for name, path in (("readiness_path", readiness_path), ("liveness_path", liveness_path)):
            if not isinstance(path, str):
                raise TypeError(f"{name} must be a str, got {type(path).__name__}")
            if not path.startswith("/"):
                raise ValueError(f"{name} must start with '/', got {path!r}")
        if readiness_path == liveness_path:
            raise ValueError(f"readiness_path and liveness_path must differ, both are {readiness_path!r}")
        if isinstance(degraded_status_code, bool) or not isinstance(degraded_status_code, int):
            raise TypeError(f"degraded_status_code must be an int, got {type(degraded_status_code).__name__}")
        if not MIN_HEALTH_STATUS_CODE <= degraded_status_code <= MAX_HEALTH_STATUS_CODE:
            raise ValueError(
                f"degraded_status_code must be between {MIN_HEALTH_STATUS_CODE} and {MAX_HEALTH_STATUS_CODE}, "
                f"got {degraded_status_code}"
            )
        if not isinstance(include_details, bool):
            raise TypeError(f"include_details must be a bool, got {type(include_details).__name__}")
        self.health_check = health_check
        self.readiness_path = readiness_path
        self.liveness_path = liveness_path
        self.degraded_status_code = degraded_status_code
        self.include_details = include_details

    async def get_readiness_answer(self) -> tuple[int, dict[str, Any]]:
        """The readiness route's answer - the connections checked now.

        Returns:
            The status and the body.
        """
        report = await self.health_check.run()
        return self.get_status_code(report.status), report.to_dict(include_details=self.include_details)

    @staticmethod
    def get_liveness_answer() -> tuple[int, dict[str, Any]]:
        """The liveness route's answer.

        Returns:
            The status and the body.
        """
        return HTTPStatus.OK.value, dict(LIVENESS_ANSWER)

    def get_status_code(self, status: HealthStatus) -> int:
        """The readiness status of a health status.

        Args:
            status: The health status.

        Returns:
            The HTTP status.
        """
        if status is HealthStatus.UNHEALTHY:
            return UNHEALTHY_STATUS_CODE
        if status is HealthStatus.DEGRADED:
            return self.degraded_status_code
        return HTTPStatus.OK.value
