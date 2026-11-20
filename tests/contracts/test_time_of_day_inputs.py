"""End-of-day clock strings cannot silently select or store midnight."""

import datetime

import pytest

from hare import Model, fields
from hare.exceptions import UnSupportedError, ValidationError
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_end_of_day_inputs_are_refused_in_writes_and_derived_time_predicates(contract_database, composite):
    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        clock = fields.TimeField()
        moment = fields.DatetimeField()

    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    moment = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    clock = datetime.time(0, tzinfo=datetime.UTC)
    await Entry.objects.create(number=1, clock=clock, moment=moment)
    for repeat in (0, 1):
        for raw in ("24:00", "T24:00", "240000", "T240000"):
            with pytest.raises(ValidationError):
                Entry(number=2, clock=raw, moment=moment)
            with pytest.raises(ValidationError):
                await Entry.objects.create(number=2, clock=raw, moment=moment)
            with pytest.raises(ValidationError):
                await Entry.objects.filter(clock=raw).values_list("number", flat=True)
            for suffix, operand in (("", raw), ("__in", [raw]), ("__not_in", [raw]), ("__range", ["00:00", raw])):
                with pytest.raises(UnSupportedError):
                    await Entry.objects.filter(**{f"moment__time{suffix}": operand}).values_list("number", flat=True)
        assert await Entry.objects.filter(moment__time="T00:00").values_list("number", flat=True) == [1]
        assert await Entry.objects.count() == 1
