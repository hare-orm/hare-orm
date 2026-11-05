import os
from collections.abc import AsyncGenerator

import pytest_asyncio

from hare.dialects.base.client.database_client import DatabaseClient
from tests.utils.multi_database_context import MultiDatabaseTestContext


@pytest_asyncio.fixture
async def two_databases() -> AsyncGenerator[tuple[DatabaseClient, DatabaseClient]]:
    """Create separate databases with the shared models and close both after each test."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with MultiDatabaseTestContext.open(
        db_url,
        ["models", "events"],
        apps={
            "models": {"models": ["tests.shared_models.multi_database_models"], "default_connection": "models"},
            "events": {"models": ["tests.shared_models.multi_database_models"], "default_connection": "events"},
        },
    ) as context:
        await context.generate_schemas()
        yield context.connections.get("models"), context.connections.get("events")
