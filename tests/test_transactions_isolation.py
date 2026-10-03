"""Transactions.atomic(isolation=...) - the transaction runs at the requested isolation level,
or at the weakest stronger one the database has."""

import pytest

from hare.contrib.test import requires_features
from hare.dialects.base.sql_dialect import SqlDialect
from hare.exceptions import QueryError, UnSupportedError
from hare.instrumentation.observer_dispatch import ObserverDispatch
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.transactions.enums import IsolationLevel
from hare.transactions.options import TransactionOptions
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


class TwoLevelDialect(SqlDialect):
    isolation_levels = (IsolationLevel.READ_COMMITTED, IsolationLevel.SERIALIZABLE)


class ReadCommittedOnlyDialect(SqlDialect):
    isolation_levels = (IsolationLevel.READ_COMMITTED,)


@pytest.fixture(autouse=True)
def _clear_query_hooks():
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()
    yield
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()


async def _record_statements(isolation, **options) -> list[str]:
    statements: list[str] = []
    Observers.observe(QueryExecuted, lambda event: statements.append(event.sql))
    async with Transactions.atomic(isolation=isolation, **options):
        await Tournament.objects.all().count()
    await Observers.wait_for_pending()
    return statements


async def _get_running_isolation_level(connection) -> str | None:
    """The level the database reports for the open transaction - None where it reports none."""
    if connection.dialect.name != "postgresql":
        return None
    _, rows = await connection.execute("SHOW transaction_isolation")
    return rows[0]["transaction_isolation"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (IsolationLevel.READ_UNCOMMITTED, IsolationLevel.READ_COMMITTED),
        (IsolationLevel.READ_COMMITTED, IsolationLevel.READ_COMMITTED),
        (IsolationLevel.REPEATABLE_READ, IsolationLevel.SERIALIZABLE),
        (IsolationLevel.SERIALIZABLE, IsolationLevel.SERIALIZABLE),
    ],
)
def test_dialect_runs_the_weakest_level_at_least_as_strong(requested, expected):
    assert TwoLevelDialect().get_isolation_level(requested) == expected


def test_dialect_refuses_a_level_stronger_than_any_it_has():
    with pytest.raises(UnSupportedError, match="serializable"):
        ReadCommittedOnlyDialect().get_isolation_level(IsolationLevel.SERIALIZABLE)


def test_isolation_given_by_name_becomes_a_level():
    assert TransactionOptions(isolation="repeatable read").isolation is IsolationLevel.REPEATABLE_READ  # type: ignore[arg-type]


@pytest.mark.parametrize("isolation", ["snapshot", 3, "SERIALIZABLE "])
def test_unknown_isolation_raises_params_error(isolation):
    with pytest.raises(QueryError, match="isolation must be one of"):
        Transactions.atomic(isolation=isolation)
    with pytest.raises(QueryError, match="isolation must be one of"):
        Transactions.atomic(isolation=isolation)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", list(IsolationLevel))
async def test_transaction_runs_at_the_requested_level(db_truncate, isolation):
    async with Transactions.atomic(isolation=isolation) as connection:
        await Tournament.objects.create(name="isolated")
        running_level = await _get_running_isolation_level(connection)
    if running_level is not None:
        assert running_level == str(isolation)
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_default_transaction_sends_no_isolation_statement(db_truncate):
    statements = await _record_statements(None)
    assert not [statement for statement in statements if "ISOLATION" in statement.upper()]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_isolation_statement_comes_first(db_truncate):
    statements = await _record_statements(IsolationLevel.SERIALIZABLE, read_only=True)
    if Tournament._meta.db.dialect.name == "postgresql":
        assert statements[0] == "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
    else:
        # Every SQLite transaction is serializable already.
        assert not [statement for statement in statements if "ISOLATION" in statement.upper()]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_isolation_does_not_outlive_the_transaction(db_truncate):
    async with Transactions.atomic(isolation=IsolationLevel.SERIALIZABLE):
        pass
    async with Transactions.atomic() as connection:
        running_level = await _get_running_isolation_level(connection)
    if running_level is not None:
        assert running_level == str(IsolationLevel.READ_COMMITTED)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_transaction_may_repeat_the_outer_level(db_truncate):
    async with Transactions.atomic(isolation=IsolationLevel.REPEATABLE_READ):
        async with Transactions.atomic(isolation="repeatable read") as nested:
            await Tournament.objects.create(name="nested")
            running_level = await _get_running_isolation_level(nested)
        async with Transactions.atomic():
            await Tournament.objects.create(name="plain nested")
    if running_level is not None:
        assert running_level == str(IsolationLevel.REPEATABLE_READ)
    assert await Tournament.objects.all().count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("outer_isolation", [None, IsolationLevel.READ_COMMITTED])
async def test_nested_transaction_with_another_level_raises(db_truncate, outer_isolation):
    async with Transactions.atomic(isolation=outer_isolation):
        with pytest.raises(QueryError, match="isolation level of the transaction it is nested in"):
            async with Transactions.atomic(isolation=IsolationLevel.SERIALIZABLE):
                pass


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_atomic_passes_isolation(db_truncate):
    @Transactions.atomic(isolation="serializable")
    async def create_tournament() -> str | None:
        await Tournament.objects.create(name="atomic")
        return await _get_running_isolation_level(Tournament.get_connection(for_write=True))

    running_level = await create_tournament()
    if running_level is not None:
        assert running_level == str(IsolationLevel.SERIALIZABLE)
    assert await Tournament.objects.all().count() == 1
