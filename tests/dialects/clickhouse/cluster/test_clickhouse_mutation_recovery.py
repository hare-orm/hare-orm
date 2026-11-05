import asyncio
from unittest.mock import AsyncMock

import pytest

from hare import Hare
from hare.dialects.base.client.enums import StatementForm
from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
from hare.dialects.clickhouse.client.clickhouse_mutation_recovery import ClickhouseMutationRecovery
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan
from hare.exceptions import OperationalError
from hare.sql.terms.parameters.query_parameters import QueryParameters

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("replicates_schema", [False, True])
def test_distributed_mutation_ids_are_never_used_for_cluster_cancellation(replicates_schema):
    placement = ClickhousePlacementPlan("book", "book storage", "library", "cluster", replicates_schema)
    assert ClickhouseMutationRecovery.for_mutation(placement, "default") is None


def test_local_mutation_recovery_keeps_its_database_despite_connection_cluster():
    placement = ClickhousePlacementPlan("book", "book", "library", "cluster")
    assert ClickhouseMutationRecovery.for_mutation(placement, "default") == ClickhouseMutationRecovery(
        "library", "book"
    )


@pytest.mark.asyncio
@pytest.mark.requires("clickhouse-connect")
async def test_recovery_uses_compiled_target_and_raw_calls_do_not_inherit_it(monkeypatch):
    exceptions = pytest.importorskip("clickhouse_connect.driver.exceptions")

    @ClickhouseClient.translate_exceptions(StatementForm.SQL)
    async def failed_write(client, query, values=None):
        raise exceptions.DatabaseError("during execution of mutations 'mutation_3.txt, mutation_2.txt'")

    context = await Hare.init(
        {
            "connections": {"default": "clickhouse+clickhouse-connect://localhost/default?cluster=connection_cluster"},
            "apps": {},
        },
        connect=False,
    )
    try:
        client = context.connections.get("default")
        execute_script = AsyncMock()
        monkeypatch.setattr(client, "execute_script", execute_script)
        recovery = ClickhouseMutationRecovery('data"base', "book storage")
        with pytest.raises(OperationalError, match="mutation_3.txt"):
            await failed_write(client, "opaque statement", recovery=recovery)
        execute_script.assert_awaited_once_with(
            "KILL MUTATION WHERE database = 'data\"base' "
            "AND table = 'book storage' AND mutation_id IN ('mutation_3.txt', 'mutation_2.txt') "
            "AND latest_fail_reason != ''"
        )
        execute_script.reset_mock()
        with pytest.raises(OperationalError):
            await failed_write(client, "opaque raw statement")
        execute_script.assert_not_awaited()
        execute_script.side_effect = RuntimeError("cleanup failed")
        with pytest.raises(OperationalError, match="mutation_3.txt"):
            await failed_write(client, "opaque statement", recovery=recovery)
    finally:
        await Hare.close_connections()
        context.__exit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.requires("clickhouse-connect")
async def test_cancelling_recovery_finishes_cleanup_without_retaining_sensitive_driver_context(monkeypatch):
    exceptions = pytest.importorskip("clickhouse_connect.driver.exceptions")

    @ClickhouseClient.translate_exceptions(StatementForm.SQL)
    async def failed_write(client, query, values=None):
        raise exceptions.DatabaseError("private-value during execution of mutation 'mutation_3.txt'")

    context = await Hare.init(
        {"connections": {"default": "clickhouse+clickhouse-connect://localhost/default"}, "apps": {}},
        connect=False,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    task = None

    async def cleanup(sql):
        entered.set()
        await release.wait()
        finished.set()

    try:
        client = context.connections.get("default")
        monkeypatch.setattr(client, "execute_script", cleanup)
        task = asyncio.create_task(
            failed_write(
                client,
                "opaque statement",
                QueryParameters(["private-value"], [0]),
                recovery=ClickhouseMutationRecovery("default", "book"),
            )
        )
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert finished.is_set()
        assert raised.value.__context__ is None
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await Hare.close_connections()
        context.__exit__(None, None, None)
