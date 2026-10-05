"""pydantic_model_creator(exclude_readonly=True) keeps a primary key the caller has to supply."""

import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError

from hare import fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.models import Model
from tests.testmodels import CharPkModel, CompositePkTriple, O2oPkProfile


def required_by_field_name(schema) -> dict[str, bool]:
    return {name: field.is_required() for name, field in schema.model_fields.items()}


@pytest.mark.asyncio
async def test_natural_char_primary_key_stays_required(db):
    CharPkCreate = pydantic_model_creator(CharPkModel, exclude_readonly=True, exclude=("peers",), name="CharPkCreate")

    assert required_by_field_name(CharPkCreate) == {"id": True}
    with pytest.raises(PydanticValidationError):
        CharPkCreate.model_validate({})
    created = await CharPkModel.objects.create(**CharPkCreate.model_validate({"id": "natural"}).model_dump())
    assert created.id == "natural"


@pytest.mark.asyncio
async def test_composite_natural_primary_key_stays_required(db):
    CompositeCreate = pydantic_model_creator(CompositePkTriple, exclude_readonly=True, name="CompositeTripleCreate")

    assert required_by_field_name(CompositeCreate) == {"a": True, "b": True, "c": True, "name": True}
    payload = CompositeCreate.model_validate({"a": 1, "b": 2, "c": 3, "name": "row"})
    created = await CompositePkTriple.objects.create(**payload.model_dump())
    assert (created.a, created.b, created.c) == (1, 2, 3)


def test_generated_and_defaulted_primary_keys_are_still_excluded():
    class GeneratedPkModel(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=10)

    class DefaultedUuidPkModel(Model):
        id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
        name = fields.CharField(max_length=10)

    class DefaultedCharPkModel(Model):
        code = fields.CharField(max_length=10, primary_key=True, default="auto")
        name = fields.CharField(max_length=10)

    for model in (GeneratedPkModel, DefaultedUuidPkModel, DefaultedCharPkModel):
        schema = pydantic_model_creator(model, exclude_readonly=True, name=f"{model.__name__}Create")
        assert required_by_field_name(schema) == {"name": True}


def test_primary_key_is_kept_without_exclude_readonly():
    NaturalPkRead = pydantic_model_creator(CharPkModel, name="CharPkRead")

    assert "id" in NaturalPkRead.model_fields


@pytest.mark.parametrize("relations_as_ids", [False, True])
def test_one_to_one_primary_key_appears_once_through_its_relation(db, relations_as_ids):
    ProfileCreate = pydantic_model_creator(
        O2oPkProfile,
        exclude_readonly=True,
        relations_as_ids=relations_as_ids,
        name=f"O2oPkProfileCreate{relations_as_ids}",
    )

    relation_field_name = "account_id" if relations_as_ids else "account"
    assert required_by_field_name(ProfileCreate) == {relation_field_name: True, "bio": True}
