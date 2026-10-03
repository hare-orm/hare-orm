"""An UPDATE setting a SQLite DecimalField from an expression stores the quantized text a plain
write stores in the UPDATE itself - no second UPDATE correcting it - and a value out of range is
still rejected."""

from decimal import Decimal

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.core.connections import Connections
from hare.exceptions import ValidationError
from hare.query.expressions import F
from tests import testmodels


async def get_stored_text(row_id: int) -> str:
    connection = Connections.get(next(iter(Connections.current().db_config)))
    _, rows = await connection.execute("SELECT decimal FROM decimalfields WHERE id = ?", [row_id])
    return dict(rows[0])["decimal"]


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_one_update_stores_the_plain_write_text(db):
    row = await testmodels.DecimalFields.objects.create(decimal=Decimal("0.1000"), decimal_nodec=1)
    async with capture_queries() as counter:
        await testmodels.DecimalFields.objects.filter(id=row.id).update(decimal=F("decimal") + Decimal("0.00007"))
    assert sum(query.lstrip().upper().startswith("UPDATE") for query in counter.queries) == 1
    plain = await testmodels.DecimalFields.objects.create(decimal=Decimal("0.1001"), decimal_nodec=1)
    assert await get_stored_text(row.id) == await get_stored_text(plain.id) == "0.1001"


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_negative_zero_and_whole_results(db):
    row = await testmodels.DecimalFields.objects.create(decimal=Decimal("2.5000"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.filter(id=row.id).update(decimal=F("decimal") * 0 - Decimal("0.00001"))
    assert await get_stored_text(row.id) == "0.0000"
    await testmodels.DecimalFields.objects.filter(id=row.id).update(decimal=F("decimal") + 3)
    assert await get_stored_text(row.id) == "3.0000"


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_value_out_of_range_is_rejected(db):
    row = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.0000"), decimal_nodec=1)
    with pytest.raises(ValidationError):
        await testmodels.DecimalFields.objects.filter(id=row.id).update(decimal=F("decimal") * Decimal("1e20"))
    assert await get_stored_text(row.id) == "1.0000"
