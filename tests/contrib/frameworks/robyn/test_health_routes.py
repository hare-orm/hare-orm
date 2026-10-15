"""HareRobyn's health routes (``health_routes=``): readiness answers the health check's status with
its code, liveness answers without the database."""

import tempfile
from typing import Any

from robyn.testing import TestClient

from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.contrib.frameworks.robyn import HareRobyn
from hare.health import HealthCheck
from tests.contrib.frameworks.health_apps import HEALTHY_CONFIG, AlwaysDegraded, make_unhealthy_config


class StartedTestClient(TestClient):
    """Robyn's test client, starting and stopping the application as its server would."""

    def __enter__(self) -> "StartedTestClient":
        self._loop.run_until_complete(self.app.start_hare())
        return self

    def __exit__(self, *args: Any) -> None:
        self._loop.run_until_complete(self.app.stop_hare())
        super().__exit__(*args)


def test_a_healthy_application_is_ready_and_alive():
    app = HareRobyn(__file__, hare_config=HEALTHY_CONFIG, health_routes=HealthRoutes(HealthCheck()))
    with StartedTestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["connections"] == {"default": {"status": "healthy"}}
        alive = client.get("/health/live")
        assert (alive.status_code, alive.json()) == (200, {"status": "alive"})


def test_an_unhealthy_application_is_not_ready_but_alive():
    with tempfile.TemporaryDirectory() as directory:
        app = HareRobyn(
            __file__, hare_config=make_unhealthy_config(directory), health_routes=HealthRoutes(HealthCheck())
        )
        with StartedTestClient(app) as client:
            ready = client.get("/health/ready")
            assert ready.status_code == 503
            assert ready.json()["connections"]["unreachable"] == {"status": "unhealthy"}
            assert client.get("/health/live").status_code == 200


def test_a_degraded_application_answers_the_status_it_was_given():
    health_routes = HealthRoutes(HealthCheck(degraded_when=[AlwaysDegraded()]), degraded_status_code=429)
    with StartedTestClient(HareRobyn(__file__, hare_config=HEALTHY_CONFIG, health_routes=health_routes)) as client:
        ready = client.get("/health/ready")
        assert (ready.status_code, ready.json()["status"]) == (429, "degraded")
