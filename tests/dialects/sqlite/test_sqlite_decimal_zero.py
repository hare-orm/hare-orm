"""SQLite keeps a decimal as its text - a negative zero was written as ``-0.00``, another text than
``0.00``: a unique column let both in, and DISTINCT and GROUP BY counted two values."""

import os
from decimal import Decimal

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import IntegrityError
from hare.models import Model
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator


class ZeroAmount(Model):
    id = fields.IntField(primary_key=True, generated=False)
    amount = fields.DecimalField(max_digits=10, decimal_places=2, unique=True)

    class Meta:
        app = "zero_amounts"
        table = "zero_amount"


@pytest.fixture(autouse=True)
def skip_without_sqlite():
    if os.getenv("HARE_TEST_DB", "sqlite").split(":")[0].split("+")[0] != "sqlite":
        pytest.skip("SQLite keeps decimals as text")


@pytest.mark.parametrize("native", [True, False], ids=["native", "python"])
@pytest.mark.asyncio
async def test_a_negative_zero_is_the_zero_it_equals(monkeypatch, native):
    if not native:
        monkeypatch.setattr(HydrateAccelerator, "module", None)
    async with hare_test_context(
        modules=["tests.dialects.sqlite.test_sqlite_decimal_zero"], app_label="zero_amounts", connection_label="models"
    ):
        await ZeroAmount.objects.create(id=1, amount=Decimal("0"))
        for refused in (
            lambda: ZeroAmount.objects.create(id=2, amount=Decimal("-0")),
            lambda: ZeroAmount.objects.bulk_create([ZeroAmount(id=3, amount=Decimal("-0.00"))]),
        ):
            with pytest.raises(IntegrityError):
                await refused()
        await ZeroAmount.objects.filter(id=1).update(amount=Decimal("-0.000"))
        assert await ZeroAmount.objects.values_list("amount", flat=True) == [Decimal("0.00")]
        assert await ZeroAmount.objects.filter(amount=Decimal("-0")).count() == 1
