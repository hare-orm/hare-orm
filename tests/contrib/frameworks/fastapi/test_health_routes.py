"""HareFastAPI's health routes (``health_routes=``): readiness answers the health check's status with
its code, liveness answers without the database, neither is in the OpenAPI schema."""

import tempfile

from fastapi.testclient import TestClient

from hare.contrib.frameworks.fastapi import HareFastAPI
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck
from tests.contrib.frameworks.health_apps import HEALTHY_CONFIG, AlwaysDegraded, make_unhealthy_config


def test_a_healthy_application_is_ready_and_alive():
    app = HareFastAPI(hare_config=HEALTHY_CONFIG, health_routes=HealthRoutes(HealthCheck()))
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "healthy"
        assert ready.json()["connections"] == {"default": {"status": "healthy"}}
        alive = client.get("/health/live")
        assert (alive.status_code, alive.json()) == (200, {"status": "alive"})
        paths = app.openapi()["paths"]
        assert "/health/ready" not in paths and "/health/live" not in paths


def test_an_unhealthy_application_is_not_ready_but_alive():
    with tempfile.TemporaryDirectory() as directory:
        app = HareFastAPI(
            hare_config=make_unhealthy_config(directory),
            health_routes=HealthRoutes(HealthCheck(), include_details=True),
        )
        with TestClient(app) as client:
            ready = client.get("/health/ready")
            assert ready.status_code == 503
            unreachable = ready.json()["connections"]["unreachable"]
            assert unreachable["status"] == "unhealthy"
            assert unreachable["error_type"] == "DBConnectionError"
            assert unreachable["reasons"] == ["the ping failed: DBConnectionError"]
            assert client.get("/health/live").status_code == 200


def test_a_degraded_application_answers_the_status_it_was_given():
    health_routes = HealthRoutes(
        HealthCheck(degraded_when=[AlwaysDegraded()]),
        readiness_path="/ready",
        liveness_path="/alive",
        degraded_status_code=429,
    )
    with TestClient(HareFastAPI(hare_config=HEALTHY_CONFIG, health_routes=health_routes)) as client:
        ready = client.get("/ready")
        assert (ready.status_code, ready.json()["status"]) == (429, "degraded")
        assert client.get("/alive").status_code == 200
