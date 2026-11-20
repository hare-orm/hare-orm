"""Refusing another init() preserves the graph and clients of waiting ClickHouse queries."""

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import ConfigurationError
from tests.dialects.clickhouse.models import Ticket
from tests.utils.connection_acquisition_gate import ConnectionAcquisitionGate
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_rejected_reinitialization_preserves_waiting_queries():
    db_url = os.getenv("HARE_TEST_DB", "")
    DatabaseUnderTest.skip_unless_clickhouse(db_url)
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.models"], db_url=db_url, app_label="models", connection_label="models"
    ) as context:
        config = {
            "connections": {"models": context.connections.db_config["models"]},
            "apps": {"models": {"models": ["tests.dialects.clickhouse.models"], "default_connection": "models"}},
        }
        apps = context.apps
        client = context.connections.get("models")

        async def count(worker: int) -> int:
            return await Ticket.objects.count()

        async def reinitialize() -> None:
            with pytest.raises(ConfigurationError, match="one application configuration"):
                await context.init(config)
            assert context.apps is apps
            assert context.connections.get("models") is client

        assert await ConnectionAcquisitionGate(workers=16).run(client, count, reinitialize) == [0] * 16
