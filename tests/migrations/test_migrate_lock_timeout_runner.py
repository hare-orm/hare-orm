"""The lock timeout on a live test database: a nested transaction can't set one, and a migration
behind a table lock another PostgreSQL session holds fails instead of waiting - atomic or not."""

import asyncio
import time

import pytest

from hare import fields
from hare.core.connections.connections import Connections
from hare.exceptions import QueryError
from hare.migrations.execution.migration_runner import MigrationRunner
from hare.migrations.migration import Migration
from hare.migrations.operations import AddField, CreateModel, DeleteModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.transactions.transactions import Transactions


@pytest.mark.asyncio
async def test_a_nested_transaction_takes_no_lock_timeout_of_its_own(db_simple):
    if not Connections.get("models").features.supports_transactions:
        pytest.skip("the database has no transactions")
    async with Transactions.atomic(lock_timeout=1):
        with pytest.raises(QueryError, match="outermost transaction"):
            async with Transactions.atomic(lock_timeout=2):
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [True, False])
async def test_postgresql_migration_fails_behind_a_table_lock(db_simple, atomic):
    connection = Connections.get("models")
    if connection.dialect.name != "postgresql":
        pytest.skip("a table lock another session holds - PostgreSQL")
    runner = MigrationRunner(connection, lock_timeout=0.3)
    state = State(models={}, apps=StateApps())
    create = Migration("0001_initial", "lock_app")
    create.operations = [
        CreateModel("LockedWidget", [("id", fields.IntField(primary_key=True))], options={"table": "locked_widget"})
    ]
    state = await MigrationRunner(connection).apply(create, state)
    add_size = Migration("0002_add_size", "lock_app")
    add_size.operations = [AddField("LockedWidget", "size", fields.IntField(default=0))]
    add_size.atomic = atomic
    lock_taken = asyncio.Event()
    release_lock = asyncio.Event()

    async def hold_lock() -> None:
        async with Transactions.autonomous("models", statement_timeout=30) as blocker:
            await blocker.execute('LOCK TABLE "locked_widget" IN ACCESS EXCLUSIVE MODE')
            lock_taken.set()
            await release_lock.wait()

    holder = asyncio.create_task(hold_lock())
    try:
        await asyncio.wait_for(lock_taken.wait(), timeout=10)
        started_at = time.perf_counter()
        with pytest.raises(Exception, match="lock timeout"):
            await runner.apply(add_size, state.clone())
        assert time.perf_counter() - started_at < 5
    finally:
        release_lock.set()
        await holder
        drop = Migration("0003_drop", "lock_app")
        drop.operations = [DeleteModel("LockedWidget")]
        await MigrationRunner(connection).apply(drop, state)
