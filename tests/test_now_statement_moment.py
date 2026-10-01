"""Now() is the moment of each statement on every backend, also inside a transaction."""

import asyncio
from datetime import UTC, datetime

import pytest

from hare.contrib.test import requires_features
from hare.query.functions.datetime import Now
from hare.transactions.transactions import Transactions
from tests import testmodels


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
