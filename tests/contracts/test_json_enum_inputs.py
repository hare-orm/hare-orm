"""Typed JSON rejects non-finite enum values before serialization can turn them into null."""

import enum

import pytest
from pydantic import BaseModel

from hare import Model, fields
from hare.exceptions import ValidationError
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar-key", "composite-key"])
@pytest.mark.parametrize(
    "field_class",
    [
        pytest.param(fields.JSONField, id="json"),
        pytest.param(fields.EncryptedJSONField, id="encrypted-json", marks=pytest.mark.requires("encryption")),
    ],
)
async def test_typed_enum_numbers_are_checked_before_json_serialization(contract_database, composite, field_class):
    class NumberChoice(enum.Enum):
        NAN = float("nan")
        INFINITY = float("inf")
        NEGATIVE_INFINITY = float("-inf")
        FINITE = 1.5

    class Document(BaseModel):
        number: NumberChoice

    class TypedDocumentRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        document = field_class(field_type=Document)

    contract_database.register_live_models([TypedDocumentRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    obj = await TypedDocumentRecord.objects.create(number=1, document=Document(number=NumberChoice.FINITE))
    for member in (NumberChoice.NAN, NumberChoice.INFINITY, NumberChoice.NEGATIVE_INFINITY):
        with pytest.raises(ValidationError, match="finite"):
            await TypedDocumentRecord.objects.create(number=2, document=Document(number=member))
        assert not await TypedDocumentRecord.objects.filter(number=2).exists()
        with pytest.raises(ValidationError, match="finite"):
            await TypedDocumentRecord.objects.filter(pk=obj.pk).update(document=Document(number=member))
        assert (await TypedDocumentRecord.objects.get(pk=obj.pk)).document.number is NumberChoice.FINITE
