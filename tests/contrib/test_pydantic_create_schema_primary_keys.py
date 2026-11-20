"""pydantic_model_creator(exclude_readonly=True) keeps a primary key the caller has to supply."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

import pytest
from pydantic import ConfigDict, ValidationError as PydanticValidationError
from pydantic.alias_generators import to_camel

from hare import fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.models import Model
from hare.query.expressions import F
from tests.shared_models.testmodels import CharPkModel, CompositePkTriple, O2oPkProfile
from tests.utils.model_bindings import ModelBindings

pytestmark = pytest.mark.usefixtures("model_conversion_context")


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


@pytest.mark.database_independent
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

    prepared_models = ModelBindings.prepare(GeneratedPkModel, DefaultedUuidPkModel, DefaultedCharPkModel)

    for model in (GeneratedPkModel, DefaultedUuidPkModel, DefaultedCharPkModel):
        schema = pydantic_model_creator(
            model, apps=prepared_models, exclude_readonly=True, name=f"{model.__name__}Create"
        )
        assert required_by_field_name(schema) == {"name": True}


@pytest.mark.database_independent
def test_primary_key_is_kept_without_exclude_readonly():
    NaturalPkRead = pydantic_model_creator(CharPkModel, name="CharPkRead")

    assert "id" in NaturalPkRead.model_fields


@pytest.mark.database_independent
@pytest.mark.parametrize("optional", (False, True))
def test_python_annotations_keep_nullable_and_optional_schema_fields(optional):
    class AnnotatedRecord(Model):
        name: str = fields.CharField(max_length=10)
        amount: int = fields.IntField()
        nullable: int = fields.IntField(null=True)
        tags: list[str] = fields.JSONField(field_type=list[str])
        data: dict[str, int] = fields.JSONField(field_type=dict[str, int], null=True)

    prepared_models = ModelBindings.prepare(AnnotatedRecord)

    schema = pydantic_model_creator(
        AnnotatedRecord,
        apps=prepared_models,
        exclude_readonly=True,
        optional=("name", "amount", "tags") if optional else (),
    )
    assert schema(name="valid", amount=2, nullable=None, tags=["valid"], data=None).model_dump() == {
        "name": "valid",
        "amount": 2,
        "nullable": None,
        "tags": ["valid"],
        "data": None,
    }
    if optional:
        assert schema(name=None, amount=None, nullable=None, tags=None, data=None).model_dump() == {
            "name": None,
            "amount": None,
            "nullable": None,
            "tags": None,
            "data": None,
        }
        assert schema().model_dump() == {"name": None, "amount": None, "nullable": None, "tags": None, "data": None}
    else:
        with pytest.raises(PydanticValidationError):
            schema(name=None, amount=None)
    with pytest.raises(PydanticValidationError):
        schema(name="longer than allowed", amount=2, tags=["valid"])
    with pytest.raises(PydanticValidationError):
        schema(name="valid", amount=2, tags=["valid"], data={"value": "invalid"})


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


@pytest.mark.database_independent
@pytest.mark.parametrize("serialize_by_alias", [False, True])
@pytest.mark.parametrize("by_alias", [None, False, True])
def test_unset_database_values_are_omitted_with_serialization_aliases(serialize_by_alias, by_alias):
    if serialize_by_alias and "serialize_by_alias" not in ConfigDict.__annotations__:
        pytest.skip("Installed pydantic has no serialize_by_alias setting")

    class FilledValues(Model):
        id = fields.IntField(primary_key=True)
        stored_count = fields.IntField(db_default=7)
        optional_count = fields.IntField(null=True)

    prepared_models = ModelBindings.prepare(FilledValues)

    schema = pydantic_model_creator(
        FilledValues,
        apps=prepared_models,
        include=("stored_count", "optional_count"),
        model_config=ConfigDict(
            alias_generator=to_camel, populate_by_name=True, serialize_by_alias=serialize_by_alias
        ),
    )
    uses_alias = serialize_by_alias if by_alias is None else by_alias
    stored_key = "storedCount" if uses_alias else "stored_count"
    optional_key = "optionalCount" if uses_alias else "optional_count"
    dump_options = {} if by_alias is None else {"by_alias": by_alias}
    assert schema.model_validate({}).model_dump(**dump_options) == {optional_key: None}
    assert schema.model_validate({"stored_count": 9}).model_dump(**dump_options) == {
        stored_key: 9,
        optional_key: None,
    }


@pytest.mark.database_independent
@pytest.mark.parametrize("field_type", ["db_default", "generated", "timestamp"])
@pytest.mark.parametrize("aliases", [False, True])
def test_unset_value_serializer_keeps_typed_output_json_schema(field_type, aliases):
    class Status(StrEnum):
        READY = "ready"
        DONE = "done"

    if field_type == "db_default":
        field = fields.IntField(db_default=7)
        value = 7
    elif field_type == "generated":
        field = fields.GeneratedField(F("base"), output_field=fields.IntField())
        value = 7
    else:
        field = fields.DatetimeField(auto_now_add=True)
        value = datetime(2020, 1, 2, tzinfo=UTC)

    class FilledRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        base = fields.IntField()
        filled_value = field
        status = fields.CharEnumField(Status)

    prepared_models = ModelBindings.prepare(FilledRecord)

    config = ConfigDict(alias_generator=to_camel, populate_by_name=True) if aliases else None
    for __ in range(2):
        schema = pydantic_model_creator(FilledRecord, apps=prepared_models, model_config=config)
        expected_fields = {to_camel(name) if aliases else name for name in ("id", "base", "filled_value", "status")}
        for mode in ("validation", "serialization"):
            document = schema.model_json_schema(mode=mode, by_alias=aliases)
            assert document["type"] == "object"
            assert document["title"]
            assert set(document["properties"]) == expected_fields
            assert document["properties"]["id"]["type"] == "integer"
            assert any(definition.get("enum") == ["ready", "done"] for definition in document["$defs"].values())
        empty = schema.model_validate({"id": 1, "base": 7, "status": "ready"})
        assert set(empty.model_dump(by_alias=aliases)) == {"id", "base", "status"}
        filled = schema.model_validate({"id": 1, "base": 7, "status": "ready", "filled_value": value})
        assert set(filled.model_dump(by_alias=aliases)) == expected_fields


@pytest.mark.database_independent
@pytest.mark.parametrize("parent_aliases", [False, True])
@pytest.mark.parametrize("serialize_by_alias", [False, True])
@pytest.mark.parametrize("by_alias", [None, False, True])
def test_inherited_unset_serializer_reads_the_childs_serialization_aliases(
    parent_aliases, serialize_by_alias, by_alias
):
    from pydantic import Field

    if serialize_by_alias and "serialize_by_alias" not in ConfigDict.__annotations__:
        pytest.skip("Installed pydantic has no serialize_by_alias setting")

    class FilledRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        stored_count = fields.IntField(db_default=7)

    prepared_models = ModelBindings.prepare(FilledRecord)

    parent = pydantic_model_creator(
        FilledRecord,
        apps=prepared_models,
        model_config=ConfigDict(
            alias_generator=to_camel if parent_aliases else None,
            populate_by_name=True,
            serialize_by_alias=serialize_by_alias,
        ),
    )

    class Child(parent):
        stored_count: int = Field(default=None, serialization_alias="customCount")

    dump_options = {} if by_alias is None else {"by_alias": by_alias}
    uses_alias = serialize_by_alias if by_alias is None else by_alias
    for rebuild in (False, True):
        if rebuild:
            Child.model_rebuild(force=True)
        assert Child(id=1).model_dump(**dump_options) == {"id": 1}
        assert Child(id=1, stored_count=7).model_dump(**dump_options) == {
            "id": 1,
            "customCount" if uses_alias else "stored_count": 7,
        }
