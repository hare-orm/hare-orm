"""Generated enum columns retain the database membership guarantee of their output field."""

from enum import Enum, IntEnum, StrEnum

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.exceptions import IntegrityError
from hare.migrations import State
from hare.query.expressions import F
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


class Level(IntEnum):
    FIRST = 1
    ALIAS = 1
    SECOND = 2


class Status(StrEnum):
    FIRST = "ready"
    ALIAS = "ready"
    SECOND = "done"


class TextNumber(Enum):
    FIRST = 1
    ALIAS = 1
    SECOND = 2


@pytest.mark.asyncio
@pytest.mark.parametrize("enum_type", [Level, Status, TextNumber])
@pytest.mark.parametrize("nullable", [False, True])
@pytest.mark.parametrize("stored", [False, True])
async def test_generated_enum_checks_reject_nonmembers_on_insert_and_update(
    contract_database, enum_type, nullable, stored
):
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if connection.features.keeps_enum_columns:
        pytest.skip("Native enum columns enforce membership without a CHECK")
    if not stored:
        async with connection.acquire_connection():
            pass
        if not connection.features.supports_virtual_generated_columns:
            pytest.skip("Virtual generated columns are not available on this server")
    is_integer = enum_type is Level
    source_field = (
        fields.IntField(null=nullable, source_field="Source Value")
        if is_integer
        else fields.CharField(max_length=20, null=nullable, source_field="Source Value")
    )
    output_field = fields.IntEnumField(enum_type) if is_integer else fields.CharEnumField(enum_type)

    class GeneratedEnumBase(Model):
        id = fields.IntField(primary_key=True, generated=False)
        source = source_field
        choice = fields.GeneratedField(
            F("source"), output_field=output_field, null=nullable, stored=stored, source_field="Computed Value"
        )

        class Meta:
            abstract = True

    class GeneratedEnumRecord(GeneratedEnumBase):
        pass

    contract_database.register_live_models([GeneratedEnumRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    valid = enum_type.ALIAS.value if is_integer or enum_type is Status else "1"
    obj = await GeneratedEnumRecord.objects.create(id=1, source=valid)
    assert obj.choice is enum_type.ALIAS
    invalid_sql = "3" if is_integer else "'x'"
    table = HareContext.require_current().apps.get_binding(GeneratedEnumRecord).db_table
    for sql in (
        f'INSERT INTO "{table}" ("id","Source Value") VALUES (2,{invalid_sql})',
        f'UPDATE "{table}" SET "Source Value"={invalid_sql} WHERE "id"=1',
    ):
        with pytest.raises(IntegrityError):
            await connection.execute_script(sql)
    assert await GeneratedEnumRecord.objects.values_list("choice", flat=True) == [enum_type.ALIAS]
    if nullable:
        assert (await GeneratedEnumRecord.objects.create(id=3, source=None)).choice is None
    snapshot = State.from_models([GeneratedEnumRecord], default_connections={CONTRACT_APP: CONTRACT_CONNECTION})
    assert any(
        constraint.name == "choice_enum_members"
        for constraint in snapshot.clone().apps.get_binding(f"{CONTRACT_APP}.GeneratedEnumRecord").constraints
    )
