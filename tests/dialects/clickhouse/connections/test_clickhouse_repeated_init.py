"""A repeated init() under load on ClickHouse: the clients it replaced were closed at once, and a
statement that had taken one of them before it opened its connection failed with "closed and dropped"
instead of running on the client replacing it."""

import asyncio
import os
import traceback

import pytest

from hare.contrib.test import hare_test_context
from tests.dialects.clickhouse.models import Ticket


@pytest.mark.asyncio
async def test_statements_running_through_a_repeated_init_all_finish():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    async with hare_test_context(
        modules=["tests.dialects.clickhouse.models"], db_url=db_url, app_label="models", connection_label="models"
    ) as context:
        config = {
            "connections": {"models": context.connections.db_config["models"]},
            "apps": {"models": {"models": ["tests.dialects.clickhouse.models"], "default_connection": "models"}},
        }
        stop = asyncio.Event()
        errors: list[BaseException] = []

        async def count_until_stopped() -> None:
            while not stop.is_set():
                try:
                    await Ticket.objects.count()
                except Exception as error:  # noqa: BLE001 - every failure is reported
                    errors.append(error)
                await asyncio.sleep(0)

        workers = [asyncio.create_task(count_until_stopped()) for _ in range(16)]
        await asyncio.sleep(0.1)
        for _ in range(20):
            await context.init(config)
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.gather(*workers)
        assert errors == [], "\n".join("".join(traceback.format_exception(error)) for error in errors)
