from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hare.contrib.test.constants import REUSE_DATABASES_ENVIRONMENT_VARIABLE
from tests.resources.test_run_resources import TestRunResources as RunResources

pytestmark = pytest.mark.database_independent


def test_workers_tag_the_original_template_and_restore_environment():
    original = {"HARE_TEST_DB": "postgresql://localhost/test_{}", REUSE_DATABASES_ENVIRONMENT_VARIABLE: "0"}
    controller_environment = dict(original)
    controller = RunResources(controller_environment)
    controller.configure()
    worker_environment = dict(controller_environment)
    inherited = dict(worker_environment)
    worker = RunResources(worker_environment)
    worker.configure()
    assert worker_environment["HARE_TEST_DB"] == f"postgresql://localhost/test_{worker.run_tag}_{{}}"
    assert worker.run_tag != controller.run_tag
    assert worker_environment[REUSE_DATABASES_ENVIRONMENT_VARIABLE] == "0"
    worker.restore_environment()
    controller.restore_environment()
    assert worker_environment == inherited
    assert controller_environment == original


@pytest.mark.asyncio
async def test_run_start_failure_closes_session_and_retains_cleanup_error():
    original, cleanup_error = RuntimeError("lock failed"), RuntimeError("close failed")
    connection = SimpleNamespace(execute=AsyncMock(side_effect=original), close=AsyncMock(side_effect=cleanup_error))
    resources = RunResources({"HARE_TEST_DB": "postgresql://localhost/test_{}"})
    resources.connect = AsyncMock(return_value=connection)
    with pytest.raises(ExceptionGroup) as error:
        await resources.start()
    assert error.value.exceptions == (original, cleanup_error)
    connection.close.assert_awaited_once()
    assert resources.connection is None


@pytest.mark.asyncio
async def test_sweep_never_drops_active_or_untagged_databases():
    resources = RunResources()
    resources.connection = SimpleNamespace(fetchval=AsyncMock(return_value=False), execute=AsyncMock())
    resources.drop_database = AsyncMock(return_value=True)
    suffix = "a" * 32
    assert not await resources.drop_database_if_orphaned(f"test_{resources.run_tag}_{suffix}")
    assert not await resources.drop_database_if_orphaned(f"test_{suffix}")
    resources.connection.fetchval.assert_not_awaited()
    assert not await resources.drop_database_if_orphaned(f"test_000000000001_{suffix}")
    resources.drop_database.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_releases_other_run_lock_even_when_drop_fails():
    resources = RunResources()
    resources.connection = SimpleNamespace(fetchval=AsyncMock(return_value=True), execute=AsyncMock())
    failure = RuntimeError("drop interrupted")
    resources.drop_database = AsyncMock(side_effect=failure)
    with pytest.raises(RuntimeError, match="drop interrupted"):
        await resources.drop_database_if_orphaned("test_000000000001_" + "b" * 32)
    resources.connection.execute.assert_awaited_once_with("SELECT pg_advisory_unlock($1)", 1)


@pytest.mark.asyncio
async def test_completed_run_closes_its_session_once():
    connection = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(return_value=[]), close=AsyncMock())
    resources = RunResources({"HARE_TEST_DB": "postgresql://localhost/test_{}"})
    resources.connect = AsyncMock(return_value=connection)
    await resources.start()
    await resources.close()
    await resources.close()
    connection.close.assert_awaited_once()
