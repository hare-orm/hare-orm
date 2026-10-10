"""ClickHouse transactions (``transactions=true``): committed and rolled back - inserts, binary bulk
inserts, mutations, lightweight deletes, cascades; the first failed statement ends the transaction; a
nested ``atomic()`` joins the enclosing transaction; a statement transactions don't take is refused."""

import pytest

from hare.exceptions import ConfigurationError, TransactionManagementError, UnSupportedError
from hare.transactions import Transactions
from hare.transactions.enums import IsolationLevel
from tests.dialects.clickhouse.transactions.models import Entry, Note


async def count_committed(table):
    """The rows a reader in a transaction of its own sees - a statement outside every transaction reads
    the rows of open transactions too."""
    connection = Entry._meta.connection
    non_transactional_client = (
        connection.get_non_transactional_client() if connection.is_transaction_client else connection
    )
    rows = await non_transactional_client.execute_dicts(
        f"SELECT count() AS rows FROM {table} SETTINGS implicit_transaction = 1"
    )
    return rows[0]["rows"]


@pytest.mark.asyncio
async def test_the_connection_has_transactions_without_savepoints(clickhouse_transactions_db):
    features = Entry._meta.connection.features
    assert features.supports_transactions and not features.supports_savepoints


@pytest.mark.asyncio
async def test_a_transaction_commits_and_rolls_back(clickhouse_transactions_db):
    async with Transactions.atomic():
        await Entry.objects.create(id=1, account="a", amount=10)
        await Entry.objects.bulk_create([Entry(id=number, account="b", amount=number) for number in range(2, 6)])
        # Seen inside the transaction, not outside it until the commit.
        assert await Entry.objects.count() == 5
        assert await count_committed("entry") == 0
    assert await count_committed("entry") == 5

    with pytest.raises(RuntimeError, match="undo"):
        async with Transactions.atomic():
            await Entry.objects.filter(account="b").update(amount=0)
            await Entry.objects.filter(id=1).delete()
            await Entry.objects.bulk_create([Entry(id=9, account="c", amount=9)])
            assert await Entry.objects.filter(amount=0).count() == 4
            raise RuntimeError("undo")
    assert await Entry.objects.order_by("id").values_list("id", "amount") == [(1, 10), (2, 2), (3, 3), (4, 4), (5, 5)]


@pytest.mark.asyncio
async def test_a_cascade_and_a_stream_run_in_the_transaction(clickhouse_transactions_db):
    entry = await Entry.objects.create(id=1, account="a", amount=1)
    await Note.objects.bulk_create([Note(id=number, entry=entry, text=str(number)) for number in range(3)])
    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            await entry.delete()
            assert await Note.objects.count() == 0
            assert [note async for note in Note.objects.stream()] == []
            raise RuntimeError("undo")
    assert await Note.objects.count() == 3
    async with Transactions.atomic():
        assert [note.text async for note in Note.objects.order_by("id").stream(chunk_size=2)] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_a_failed_statement_ends_the_transaction(clickhouse_transactions_db):
    with pytest.raises(TransactionManagementError, match="nothing was committed"):
        async with Transactions.atomic() as connection:
            await Entry.objects.create(id=1, account="a", amount=1)
            with pytest.raises(Exception, match="stop"):
                await connection.execute_dicts("SELECT throwIf(1, 'stop')")
            with pytest.raises(TransactionManagementError, match="rolled the transaction back"):
                await Entry.objects.create(id=2, account="a", amount=2)
    assert await count_committed("entry") == 0


@pytest.mark.asyncio
async def test_a_nested_atomic_joins_the_transaction(clickhouse_transactions_db):
    async with Transactions.atomic():
        await Entry.objects.create(id=1, account="a", amount=1)
        async with Transactions.atomic():
            await Entry.objects.create(id=2, account="a", amount=2)
    assert await count_committed("entry") == 2
    # An error leaving a nested block rolls the whole transaction back - its rows can't be undone alone.
    with pytest.raises(TransactionManagementError, match="no savepoints"):
        async with Transactions.atomic():
            await Entry.objects.create(id=3, account="a", amount=3)
            try:
                async with Transactions.atomic():
                    await Entry.objects.create(id=4, account="a", amount=4)
                    raise ValueError("inner")
            except ValueError:
                pass
    assert await Entry.objects.order_by("id").values_list("id", flat=True) == [1, 2]


@pytest.mark.asyncio
async def test_a_statement_transactions_dont_take_is_refused(clickhouse_transactions_db):
    connection = Entry._meta.connection
    await connection.execute_script(
        "CREATE TABLE replicated_entry (id Int64) ENGINE = "
        f"ReplicatedMergeTree('/hare/tests/{connection.database}/replicated_entry', 'replica') ORDER BY id"
    )
    try:
        with pytest.raises(TransactionManagementError):
            async with Transactions.atomic() as transaction:
                with pytest.raises(UnSupportedError, match="no transaction"):
                    await transaction.execute_script("INSERT INTO replicated_entry VALUES (1)")
    finally:
        await connection.execute_script("DROP TABLE replicated_entry SYNC")


@pytest.mark.asyncio
async def test_a_server_without_transactions_is_refused_on_connect(clickhouse_transactions_db, monkeypatch):
    connection = Entry._meta.connection
    client = type(connection)(
        connection_alias="no_transactions",
        host=connection.host,
        port=connection.port,
        user=connection.user,
        password=connection.password,
        database=connection.database,
        transactions=True,
    )
    error_class = (
        client.driver_errors.driver[0]
        if isinstance(client.driver_errors.driver, tuple)
        else client.driver_errors.driver
    )

    async def refuse():
        raise error_class("Transactions are not supported")

    monkeypatch.setattr(client, "check_transactions", refuse)
    with pytest.raises(ConfigurationError, match="allow_experimental_transactions"):
        await client.create_connection(with_db=True)
    plain = type(connection)(
        connection_alias="plain",
        host=connection.host,
        port=connection.port,
        user=connection.user,
        password=connection.password,
        database=connection.database,
    )
    try:
        await plain.create_connection(with_db=True)
        assert not plain.features.supports_transactions
    finally:
        await plain.close()


@pytest.mark.asyncio
async def test_a_transaction_reads_its_snapshot(clickhouse_transactions_db):
    # Any level up to REPEATABLE READ runs at the snapshot the transaction began with; no stronger one.
    async with Transactions.atomic(isolation=IsolationLevel.READ_COMMITTED):
        await Entry.objects.create(id=1, account="a", amount=1)
    assert await count_committed("entry") == 1
    with pytest.raises(UnSupportedError, match="serializable"):
        async with Transactions.atomic(isolation=IsolationLevel.SERIALIZABLE):
            pass
    with pytest.raises(UnSupportedError, match="read-only"):
        async with Transactions.atomic(read_only=True):
            pass
