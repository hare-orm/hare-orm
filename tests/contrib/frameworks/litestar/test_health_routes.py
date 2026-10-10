"""HarePlugin's health routes (``health_routes=``): readiness answers the health check's status with
its code, liveness answers without the database, neither is in the OpenAPI schema."""

import tempfile

import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.contrib.frameworks.litestar import HarePlugin
from hare.health import HealthCheck
from tests.contrib.frameworks.health_apps import HEALTHY_CONFIG, AlwaysDegraded, make_unhealthy_config


@pytest.mark.asyncio
async def test_a_healthy_application_is_ready_and_alive():
    app = Litestar([], plugins=[HarePlugin(HEALTHY_CONFIG, health_routes=HealthRoutes(HealthCheck()))])
    async with AsyncTestClient(app) as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "healthy",
            "checked_at": ready.json()["checked_at"],
            "connections": {"default": {"status": "healthy"}},
        }
        alive = await client.get("/health/live")
        assert (alive.status_code, alive.json()) == (200, {"status": "alive"})
        paths = app.openapi_schema.paths or {}
        assert "/health/ready" not in paths and "/health/live" not in paths


@pytest.mark.asyncio
async def test_an_unhealthy_application_is_not_ready_but_alive():
    with tempfile.TemporaryDirectory() as directory:
        plugin = HarePlugin(make_unhealthy_config(directory), health_routes=HealthRoutes(HealthCheck()))
        async with AsyncTestClient(Litestar([], plugins=[plugin])) as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 503
            assert ready.json()["connections"]["unreachable"] == {"status": "unhealthy"}
            assert (await client.get("/health/live")).status_code == 200


@pytest.mark.asyncio
async def test_a_degraded_application_answers_the_status_it_was_given():
    health_routes = HealthRoutes(HealthCheck(degraded_when=[AlwaysDegraded()]), degraded_status_code=429)
    async with AsyncTestClient(
        Litestar([], plugins=[HarePlugin(HEALTHY_CONFIG, health_routes=health_routes)])
    ) as client:
        ready = await client.get("/health/ready")
        assert (ready.status_code, ready.json()["status"]) == (429, "degraded")
