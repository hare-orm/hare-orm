"""The deferral of PROTECT backstops a hard delete runs in lasts for its block only - a block that
fails takes it back too, so a later statement of the same transaction is checked right away."""

import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.schema.runtime_statements.postgresql_foreign_key_deferral import (
    PostgresqlForeignKeyDeferral,
)
from hare.exceptions import IntegrityError
from hare.transactions.transactions import Transactions
from tests.testmodels import SoftDeleteChildProtect, SoftDeleteParent


class BlockFailed(Exception):
    pass


async def delete_parent_row(db, parent: SoftDeleteParent) -> None:
    table = SoftDeleteParent._meta.db_table
    await db.execute(f'DELETE FROM "{table}" WHERE "id" = $1', [parent.id])


@requires_features(checks_foreign_keys_per_cascade_step=True, supports_transactions=True)
@pytest.mark.asyncio
async def test_a_failed_block_takes_the_deferral_back(db_truncate):
    parent = await SoftDeleteParent.objects.create(name="parent")
    await SoftDeleteChildProtect.objects.create(name="child", parent=parent)
    async with Transactions.atomic() as transaction:
        with pytest.raises(BlockFailed):
            async with PostgresqlForeignKeyDeferral.defer(SoftDeleteParent, transaction) as deferred:
                assert deferred
                raise BlockFailed
        with pytest.raises(IntegrityError):
            async with Transactions.atomic() as savepoint:
                await delete_parent_row(savepoint, parent)
    assert await SoftDeleteParent.objects.filter(id=parent.id).exists()


@requires_features(checks_foreign_keys_per_cascade_step=True, supports_transactions=True)
@pytest.mark.asyncio
async def test_a_block_failing_on_a_database_error_keeps_that_error(db_truncate):
    parent = await SoftDeleteParent.objects.create(name="parent")
    await SoftDeleteChildProtect.objects.create(name="child", parent=parent)
    async with Transactions.atomic():
        with pytest.raises(IntegrityError, match="duplicate key|unique"):
            async with Transactions.atomic() as savepoint:
                async with PostgresqlForeignKeyDeferral.defer(SoftDeleteParent, savepoint) as deferred:
                    assert deferred
                    table = SoftDeleteParent._meta.db_table
                    await savepoint.execute(
                        f'INSERT INTO "{table}" ("id", "name") VALUES ($1, $2)', [parent.id, "duplicate"]
                    )
        with pytest.raises(IntegrityError):
            async with Transactions.atomic() as savepoint:
                await delete_parent_row(savepoint, parent)
