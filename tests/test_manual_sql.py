import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions


@pytest.mark.asyncio
async def test_simple_insert(db_truncate):
    """Test simple INSERT via raw SQL."""
    conn = Connections.get("models")
    await conn.execute("INSERT INTO author (name) VALUES ('Foo')")
    assert await conn.execute_dicts("SELECT name FROM author") == [{"name": "Foo"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_in_transaction(db_truncate):
    """Test INSERT inside transaction context manager."""
    async with Transactions.atomic() as conn:
        await conn.execute("INSERT INTO author (name) VALUES ('Foo')")

    conn = Connections.get("models")
    assert await conn.execute_dicts("SELECT name FROM author") == [{"name": "Foo"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_in_transaction_exception(db_truncate):
    """Test that transaction rolls back on exception."""
    try:
        async with Transactions.atomic() as conn:
            await conn.execute("INSERT INTO author (name) VALUES ('Foo')")
            raise ValueError("oops")
    except ValueError:
        pass

    conn = Connections.get("models")
    assert await conn.execute_dicts("SELECT name FROM author") == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_in_transaction_rollback(db_truncate):
    """Test explicit rollback inside transaction."""
    async with Transactions.atomic() as conn:
        await conn.execute("INSERT INTO author (name) VALUES ('Foo')")
        await conn.rollback()

    conn = Connections.get("models")
    assert await conn.execute_dicts("SELECT name FROM author") == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_in_transaction_commit(db_truncate):
    """Test explicit commit inside transaction persists data even on exception."""
    try:
        async with Transactions.atomic() as conn:
            await conn.execute("INSERT INTO author (name) VALUES ('Foo')")
            await conn.commit()
            raise ValueError("oops")
    except ValueError:
        pass

    conn = Connections.get("models")
    assert await conn.execute_dicts("SELECT name FROM author") == [{"name": "Foo"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_transaction_manual_rollback_only_undoes_the_savepoint(db_truncate):
    """A manual `await conn.rollback()` called on a NESTED transaction (obtained via a second
    `Transactions.atomic()` while already inside one) used to issue a real,
    connection-level ROLLBACK on sqlite/rust_pg instead of ROLLBACK TO SAVEPOINT - silently
    discarding the still-open OUTER transaction's own work too, even though the outer `async
    with` block never raised and went on to commit normally."""
    async with Transactions.atomic() as outer:
        await outer.execute("INSERT INTO author (name) VALUES ('outer-row')")
        async with Transactions.atomic() as inner:
            await inner.execute("INSERT INTO author (name) VALUES ('inner-row')")
            await inner.rollback()
        await outer.execute("INSERT INTO author (name) VALUES ('after-nested-rollback')")

    conn = Connections.get("models")
    names = {row["name"] for row in await conn.execute_dicts("SELECT name FROM author")}
    assert names == {"outer-row", "after-nested-rollback"}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_transaction_manual_commit_stays_subordinate_to_outer_rollback(db_truncate):
    """A manual `await conn.commit()` called on a NESTED transaction used to issue a real,
    connection-level COMMIT on sqlite/rust_pg instead of RELEASE SAVEPOINT - a savepoint release
    is not a real commit, so its row must still be undone if the still-open OUTER transaction
    later rolls back."""
    try:
        async with Transactions.atomic() as outer:
            await outer.execute("INSERT INTO author (name) VALUES ('outer-row')")
            async with Transactions.atomic() as inner:
                await inner.execute("INSERT INTO author (name) VALUES ('inner-row')")
                await inner.commit()
            raise ValueError("force outer rollback")
    except ValueError:
        pass

    conn = Connections.get("models")
    assert await conn.execute_dicts("SELECT name FROM author") == []
