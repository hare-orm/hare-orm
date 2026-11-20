"""Enum member and integer input forms use the same declared stored code."""

from enum import IntEnum

import pytest

from hare import Model, fields
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.permuted_integer_enum import PermutedIntegerEnum


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_integer_enum_input_forms_keep_the_declared_member_in_filters_and_writes(contract_database, composite):
    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        status = fields.IntEnumField(PermutedIntegerEnum)

    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Entry.objects.bulk_create(
        [Entry(number=1, status=PermutedIntegerEnum.FIRST), Entry(number=2, status=PermutedIntegerEnum.SECOND)]
    )
    foreign_enum = IntEnum("ForeignCodes", {"FIRST": 1, "SECOND": 2})
    for repeat in (0, 1):
        for member in PermutedIntegerEnum:
            for value in (member, member.value, foreign_enum(member.value)):
                assert await Entry.objects.filter(status=value).values_list("number", flat=True) == [member.value]
                assert await Entry.objects.filter(status__in=[value] * 32).values_list("number", flat=True) == [
                    member.value
                ]
        for value in (PermutedIntegerEnum.FIRST, 1, foreign_enum.FIRST):
            await Entry.objects.filter(number=1).update(status=value)
            assert (await Entry.objects.get(number=1)).status is PermutedIntegerEnum.FIRST
