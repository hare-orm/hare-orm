"""The health routes every framework integration adds (``HealthRoutes``): their arguments are checked,
and each health status gets its status code."""

import pytest

from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck, HealthStatus


@pytest.mark.parametrize(
    ("arguments", "error_type", "message"),
    [
        ({"health_check": "check"}, TypeError, "health_check must be a HealthCheck"),
        ({"readiness_path": "health/ready"}, ValueError, "readiness_path must start with '/'"),
        ({"liveness_path": 5}, TypeError, "liveness_path must be a str"),
        ({"liveness_path": "/health/ready"}, ValueError, "must differ"),
        ({"degraded_status_code": 199}, ValueError, "between 200 and 599"),
        ({"degraded_status_code": "503"}, TypeError, "degraded_status_code must be an int"),
        ({"degraded_status_code": True}, TypeError, "degraded_status_code must be an int"),
        ({"include_details": 1}, TypeError, "include_details must be a bool"),
    ],
)
def test_the_arguments_are_checked(arguments, error_type, message):
    with pytest.raises(error_type, match=message):
        HealthRoutes(**{"health_check": HealthCheck(), **arguments})


def test_each_health_status_gets_its_status_code():
    health_routes = HealthRoutes(HealthCheck(), degraded_status_code=429)
    assert health_routes.get_status_code(HealthStatus.HEALTHY) == 200
    assert health_routes.get_status_code(HealthStatus.DEGRADED) == 429
    assert health_routes.get_status_code(HealthStatus.UNHEALTHY) == 503
    assert HealthRoutes.get_liveness_answer() == (200, {"status": "alive"})
    assert (health_routes.readiness_path, health_routes.liveness_path) == ("/health/ready", "/health/live")
