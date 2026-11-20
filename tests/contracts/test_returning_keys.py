"""Returning rows use the same key identity as the database's stored rows."""

import math

import pytest

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.query.expressions import F
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
@pytest.mark.parametrize("composite", [False, True], ids=["scalar-key", "composite-key"])
@pytest.mark.parametrize("named", [False, True], ids=["instances", "named-fields"])
async def test_update_and_soft_delete_return_rows_with_non_reflexive_keys(contract_database, composite, named):
    class FloatKeyRecord(Model):
        code = fields.FloatField(primary_key=not composite, source_field="key_code")
        region = fields.IntField(default=7, source_field="key_region")
        value = fields.IntField()
        deleted_at = fields.DatetimeField(null=True)
        if composite:
            pk = fields.CompositePrimaryKey("region", "code")

        class Meta:
            soft_delete_field = "deleted_at"

    contract_database.register_live_models([FloatKeyRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await FloatKeyRecord.objects.create(code=float("nan"), value=10)
    await FloatKeyRecord.objects.create(code=2.5, value=20)
    field_names = ("code", "value", "deleted_at") if named else ()
    for increment in (1, 2):
        returned = await FloatKeyRecord.objects.update(value=F("value") + 1).returning(*field_names)
        values = sorted((row["value"], row["code"]) if named else (row.value, row.code) for row in returned)
        assert len(values) == 2
        assert values[0][0] == 10 + increment and math.isnan(values[0][1])
        assert values[1] == (20 + increment, 2.5)
        assert await FloatKeyRecord.objects.order_by("value").values_list("value", flat=True) == [
            10 + increment,
            20 + increment,
        ]
    deleted = await FloatKeyRecord.objects.filter(value=12).delete().returning(*field_names)
    assert len(deleted) == 1
    row = deleted[0]
    assert math.isnan(row["code"] if named else row.code)
    assert (row["value"] if named else row.value) == 12
    assert (row["deleted_at"] if named else row.deleted_at) is not None
    assert await FloatKeyRecord.objects.values_list("value", flat=True) == [22]
    assert await FloatKeyRecord.objects.include_deleted().count() == 2
