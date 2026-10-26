"""A date or a moment shifted beyond the ones a backend holds raises instead of giving another value."""

import datetime
from decimal import Decimal

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError
from hare.query.expressions import F
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels_postgres import ExpressionTypeRow


@pytest.mark.postgresql_models
@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_shift_beyond_the_held_dates_is_refused(db):
    """SQLite reported Python's overflow as 'string or blob too big'."""
    await ExpressionTypeRow.objects.create(
        num=1,
        fl=1.0,
        dec=Decimal("1"),
        dec3=Decimal("0"),
        d=datetime.date(9999, 1, 2),
        dt=datetime.datetime(9999, 1, 2, tzinfo=datetime.UTC),
        td=datetime.timedelta(0),
        s="x",
        flag=True,
    )

    for expression in (F("d") + datetime.timedelta(days=400), F("dt") + datetime.timedelta(days=400)):
        with pytest.raises(OperationalError):
            # A savepoint - a failed statement aborts the transaction around it on Postgres.
            async with Transactions.atomic():
                await ExpressionTypeRow.objects.annotate(later=expression).values_list("later", flat=True)
