"""Now() is the moment of each statement on every backend, also inside a transaction."""

import asyncio
from datetime import UTC, datetime

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.query.functions.datetime import Now
from hare.transactions.transactions import Transactions
from tests.shared_models import testmodels


@pytest.mark.timing
@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_now_differs_between_statements_of_a_transaction(db):
    first = await testmodels.DatetimeFields.objects.create(datetime=datetime(2000, 1, 1, tzinfo=UTC))
    second = await testmodels.DatetimeFields.objects.create(datetime=datetime(2000, 1, 1, tzinfo=UTC))
    async with Transactions.atomic():
        await testmodels.DatetimeFields.objects.filter(id=first.id).update(datetime=Now())
        await asyncio.sleep(0.02)
        await testmodels.DatetimeFields.objects.filter(id=second.id).update(datetime=Now())
    moments = dict(await testmodels.DatetimeFields.objects.all().values_list("id", "datetime"))
    assert moments[second.id] > moments[first.id]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_now_is_evaluated_by_each_update_statement(db):
    previous = datetime(2000, 1, 1, tzinfo=UTC)
    first = await testmodels.DatetimeFields.objects.create(datetime=previous)
    second = await testmodels.DatetimeFields.objects.create(datetime=previous)
    async with Transactions.atomic():
        async with capture_queries() as counter:
            await testmodels.DatetimeFields.objects.filter(id=first.id).update(datetime=Now())
            await testmodels.DatetimeFields.objects.filter(id=second.id).update(datetime=Now())
    statements = [sql.upper() for sql in counter.queries if sql.lstrip().upper().startswith("UPDATE ")]
    assert len(statements) == 2
    required_sql = {
        "postgresql": ("STATEMENT_TIMESTAMP()",),
        "sqlite": ("STRFTIME(", "'NOW'"),
        "clickhouse": ("NOW64(",),
    }[testmodels.DatetimeFields._meta.connection.dialect.name]
    for sql in statements:
        assert all(fragment in sql for fragment in required_sql), sql
    moments = dict(await testmodels.DatetimeFields.objects.all().values_list("id", "datetime"))
    assert set(moments) == {first.id, second.id}
    assert all(isinstance(moment, datetime) and moment != previous for moment in moments.values())
