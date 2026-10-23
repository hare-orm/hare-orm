"""A DELETE whose condition reads a set operation runs as an ``ALTER TABLE ... DELETE`` mutation - a
lightweight DELETE fails on one - and a mutation that fails is stopped, so the next write of the table
runs and the failed one never changes its rows later."""

import pytest

from hare.exceptions import OperationalError
from tests.dialects.clickhouse.models import Account, Revision


async def create_revisions():
    await Revision.objects.bulk_create(
        [Revision(document_id=1, number=number, title=f"r{number}", score=float(number)) for number in (1, 2, 3)]
    )


@pytest.mark.asyncio
async def test_a_delete_by_keys_of_a_union(clickhouse_db):
    await create_revisions()

    keys = Revision.objects.filter(number=1).values("pk").union(Revision.objects.filter(number=2).values("pk"))
    await Revision.objects.filter(pk__in=keys).delete()
    assert await Revision.objects.values_list("number", flat=True) == [3]


@pytest.mark.asyncio
async def test_a_failed_mutation_leaves_the_table_writable(clickhouse_db):
    await Account.objects.create(id=1, owner="a", balance=1)
    connection = Account._meta.connection

    with pytest.raises(OperationalError):
        await connection.execute_script("ALTER TABLE account UPDATE balance = throwIf(1, 'failed') WHERE 1")
    with pytest.raises(OperationalError):
        await connection.execute("ALTER TABLE account UPDATE balance = throwIf(1, 'failed') WHERE 1")
    rows = await connection.execute_dicts(
        "SELECT count() AS running FROM system.mutations WHERE database = currentDatabase() AND NOT is_done"
    )
    assert rows[0]["running"] == 0
    await Account.objects.filter(id=1).update(balance=5)
    assert await Account.objects.values_list("balance", flat=True) == [5]


@pytest.mark.asyncio
async def test_a_failed_statement_names_the_error_without_the_servers_stack_trace(clickhouse_db):
    """The native protocol's error text carried the server's multi-line stack trace."""
    with pytest.raises(OperationalError) as error:
        await Account._meta.connection.execute("SELECT throwIf(1, 'failed on purpose')")
    assert "failed on purpose" in str(error.value)
    assert "Stack trace" not in str(error.value)
