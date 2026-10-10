"""``stream()`` on ClickHouse - the rows read as the server sends them, outside a transaction: model
instances, values, a combined query; a reader stopping early leaves no statement running and no
connection taken; a failing query raises hare's error."""

import asyncio

import pytest

from hare.exceptions import OperationalError
from tests.dialects.clickhouse.models import Account, Team


async def create_accounts(count):
    await Account.objects.bulk_create(
        [Account(id=number, owner=f"owner{number % 7}", balance=number) for number in range(count)]
    )


async def get_running_streams(connection):
    rows = await connection.execute_dicts(
        "SELECT count() AS running FROM system.processes WHERE query LIKE '%FROM \"account\"%' "
        "AND query NOT LIKE '%system.processes%'"
    )
    return rows[0]["running"]


@pytest.mark.asyncio
async def test_rows_are_streamed_outside_a_transaction(clickhouse_db):
    await create_accounts(2500)
    balances = [account.balance async for account in Account.objects.order_by("id").stream(chunk_size=300)]
    assert balances == list(range(2500))
    owners = [row async for row in Account.objects.filter(balance__lt=10).order_by("id").values("owner").stream()]
    assert owners == [{"owner": f"owner{number % 7}"} for number in range(10)]
    numbers = [
        number
        async for number in Account.objects.filter(id__lt=3)
        .values_list("id", flat=True)
        .union(Account.objects.filter(id__gt=2497).values_list("id", flat=True))
        .order_by("id")
        .stream(chunk_size=2)
    ]
    assert numbers == [0, 1, 2, 2498, 2499]
    assert [team async for team in Team.objects.filter(name="none").stream()] == []


@pytest.mark.asyncio
async def test_a_reader_stopping_early_leaves_nothing_running(clickhouse_db):
    connection = Account._meta.connection
    await create_accounts(20000)
    for _ in range(3):
        read = 0
        async for _account in Account.objects.order_by("id").stream(chunk_size=100):
            read += 1
            if read == 150:
                break
        assert read == 150
    # The statements of the readers that stopped are no longer running on the server.
    for _ in range(50):
        if await get_running_streams(connection) == 0:
            break
        await asyncio.sleep(0.1)
    assert await get_running_streams(connection) == 0
    # The connection serves the next statements - none is left taken.
    counts = await asyncio.gather(*(Account.objects.filter(balance__gte=number).count() for number in range(20)))
    assert counts == [20000 - number for number in range(20)]


@pytest.mark.asyncio
async def test_a_failing_stream_raises_hares_error(clickhouse_db):
    connection = Account._meta.connection
    with pytest.raises(OperationalError):
        async for _row in connection.stream(
            "SELECT throwIf(number = 5000, 'stop') FROM numbers(10000)", chunk_size=100
        ):
            pass
    # And the connection still serves statements.
    assert (await connection.execute_dicts("SELECT 1 AS one"))[0]["one"] == 1
