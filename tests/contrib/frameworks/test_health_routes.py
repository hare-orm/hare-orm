"""The health routes every framework integration adds (``HealthRoutes``): each health status gets its
status code."""

import pytest

from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck, HealthStatus

REQUIRES = ("pydantic",)


@pytest.mark.database_independent
def test_each_health_status_gets_its_status_code():
    health_routes = HealthRoutes(HealthCheck(), degraded_status_code=429)
    assert health_routes.get_status_code(HealthStatus.HEALTHY) == 200
    assert health_routes.get_status_code(HealthStatus.DEGRADED) == 429
    assert health_routes.get_status_code(HealthStatus.UNHEALTHY) == 503
    assert HealthRoutes.get_liveness_answer() == (200, {"status": "alive"})
    assert (health_routes.readiness_path, health_routes.liveness_path) == ("/health/ready", "/health/live")
