import os
import uuid

import pytest
import pytest_asyncio
from pydantic import ValidationError

from hare.contrib.pydantic import PydanticModel, pydantic_model_creator, pydantic_queryset_creator
from hare.contrib.test import assert_num_queries
from hare.contrib.test.helpers import hare_test_context
from tests.contrib.models_pydantic_relation_ids import (
    RelationIdCodeTarget,
    RelationIdItem,
    RelationIdOwner,
    RelationIdOwnerDetail,
    RelationIdProfile,
    RelationIdTag,
    RelationIdUuidTarget,
)


@pytest_asyncio.fixture
async def db_relation_ids():
    async with hare_test_context(
        modules=["tests.contrib.models_pydantic_relation_ids"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


async def create_item() -> RelationIdItem:
    owner = await RelationIdOwner.objects.create(id=1, name="owner")
    reviewer = await RelationIdOwner.objects.create(id=2, name="reviewer")
    uuid_target = await RelationIdUuidTarget.objects.create()
    code_target = await RelationIdCodeTarget.objects.create(code="ABC")
    item = await RelationIdItem.objects.create(
        id=10,
        title="item",
        owner=owner,
        reviewer=reviewer,
        uuid_target=uuid_target,
        code_target=code_target,
        auditor=owner,
    )
    tag = await RelationIdTag.objects.create(id=5, name="tag", owner=owner)
    await item.tags.add(tag)
    return item


@pytest.mark.asyncio
async def test_forward_fk_becomes_id_field(db_relation_ids):
    schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    assert list(schema.model_fields) == [
        "id",
        "title",
        "owner_id",
        "reviewer_id",
        "uuid_target_id",
        "code_target_id",
        "auditor_id",
        "tags",
        "created",
    ]
    assert not {"owner", "reviewer", "uuid_target", "code_target", "auditor"} & set(schema.model_fields)


@pytest.mark.asyncio
async def test_required_and_nullable_fk_ids(db_relation_ids):
    schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    json_schema = schema.model_json_schema()
    assert "owner_id" in json_schema["required"]
    assert "reviewer_id" not in json_schema["required"]
    assert json_schema["properties"]["owner_id"]["type"] == "integer"
    assert {"type": "null"} in json_schema["properties"]["reviewer_id"]["anyOf"]
    payload = {"id": 1, "title": "t", "owner_id": 3, "tags": [], "created": "2026-01-01T00:00:00Z"}
    validated = schema.model_validate(payload)
    assert validated.owner_id == 3
    assert validated.reviewer_id is None
    with pytest.raises(ValidationError):
        schema.model_validate({**payload, "owner_id": None})
    missing_owner = dict(payload)
    del missing_owner["owner_id"]
    with pytest.raises(ValidationError):
        schema.model_validate(missing_owner)


@pytest.mark.asyncio
async def test_non_int_target_pk_types(db_relation_ids):
    schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    json_schema = schema.model_json_schema()
    uuid_property = json_schema["properties"]["uuid_target_id"]
    assert {"type": "string", "format": "uuid"} in uuid_property["anyOf"]
    code_property = json_schema["properties"]["code_target_id"]
    assert {"type": "string", "maxLength": 16} in code_property["anyOf"]
    payload = {"id": 1, "title": "t", "owner_id": 3, "tags": [], "created": "2026-01-01T00:00:00Z"}
    target_uuid = uuid.uuid4()
    validated = schema.model_validate({**payload, "uuid_target_id": str(target_uuid), "code_target_id": "X"})
    assert validated.uuid_target_id == target_uuid
    assert validated.code_target_id == "X"
    with pytest.raises(ValidationError):
        schema.model_validate({**payload, "uuid_target_id": "not-a-uuid"})
    with pytest.raises(ValidationError):
        schema.model_validate({**payload, "code_target_id": "X" * 17})


@pytest.mark.asyncio
async def test_one_to_one_becomes_id_field(db_relation_ids):
    schema = pydantic_model_creator(RelationIdProfile, relations_as_ids=True)
    assert list(schema.model_fields) == ["id", "bio", "owner_id", "backup_owner_id"]
    json_schema = schema.model_json_schema()
    assert "owner_id" in json_schema["required"]
    assert "backup_owner_id" not in json_schema["required"]
    with pytest.raises(ValidationError):
        schema.model_validate({"id": 1, "owner_id": None})
    assert schema.model_validate({"id": 1, "owner_id": 2}).backup_owner_id is None


@pytest.mark.asyncio
async def test_one_to_one_primary_key_becomes_single_id_field(db_relation_ids):
    schema = pydantic_model_creator(RelationIdOwnerDetail, relations_as_ids=True)
    assert list(schema.model_fields) == ["owner_id", "note"]
    create_schema = pydantic_model_creator(RelationIdOwnerDetail, relations_as_ids=True, exclude_readonly=True)
    assert list(create_schema.model_fields) == ["owner_id", "note"]
    assert create_schema.model_json_schema()["required"] == ["owner_id"]
    excluded_by_relation = pydantic_model_creator(RelationIdOwnerDetail, relations_as_ids=True, exclude=("owner",))
    assert list(excluded_by_relation.model_fields) == ["owner_id", "note"]


@pytest.mark.asyncio
async def test_exclude_accepts_relation_and_id_names(db_relation_ids):
    by_relation = pydantic_model_creator(RelationIdItem, relations_as_ids=True, exclude=("owner", "tags"))
    by_id = pydantic_model_creator(RelationIdItem, relations_as_ids=True, exclude=("owner_id", "tags"))
    assert "owner_id" not in by_relation.model_fields
    assert list(by_relation.model_fields) == list(by_id.model_fields)
    assert "reviewer_id" in by_relation.model_fields


@pytest.mark.asyncio
async def test_include_accepts_relation_and_id_names(db_relation_ids):
    by_relation = pydantic_model_creator(RelationIdItem, relations_as_ids=True, include=("title", "owner"))
    by_id = pydantic_model_creator(RelationIdItem, relations_as_ids=True, include=("title", "owner_id"))
    assert list(by_relation.model_fields) == ["title", "owner_id"]
    assert list(by_id.model_fields) == ["title", "owner_id"]


@pytest.mark.asyncio
async def test_optional_accepts_relation_and_id_names(db_relation_ids):
    for optional in (("owner",), ("owner_id",)):
        schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True, optional=optional)
        assert "owner_id" not in schema.model_json_schema()["required"]
        validated = schema.model_validate({"id": 1, "title": "t", "tags": [], "created": "2026-01-01T00:00:00Z"})
        assert validated.owner_id is None
        assert "owner_id" not in validated.model_dump(exclude_unset=True)


@pytest.mark.asyncio
async def test_exclude_readonly_keeps_id_fields(db_relation_ids):
    schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True, exclude_readonly=True, exclude=("tags",))
    assert list(schema.model_fields) == [
        "title",
        "owner_id",
        "reviewer_id",
        "uuid_target_id",
        "code_target_id",
        "auditor_id",
    ]
    assert schema.model_json_schema()["required"] == ["title", "owner_id"]
    owner = await RelationIdOwner.objects.create(id=7, name="o")
    created = await RelationIdItem.objects.create(
        **schema.model_validate({"title": "t", "owner_id": owner.id}).model_dump()
    )
    assert (await RelationIdItem.objects.get(id=created.id)).owner_id == 7


@pytest.mark.asyncio
async def test_exclude_sensitive_drops_sensitive_fk_id(db_relation_ids):
    public_schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True, exclude_sensitive=True)
    full_schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    assert "auditor_id" not in public_schema.model_fields
    assert "auditor_id" in full_schema.model_fields


@pytest.mark.asyncio
async def test_reverse_relations_left_out(db_relation_ids):
    schema = pydantic_model_creator(RelationIdOwner, relations_as_ids=True)
    assert list(schema.model_fields) == ["id", "name"]
    nested_schema = pydantic_model_creator(RelationIdOwner)
    assert {"items", "profile", "detail"} <= set(nested_schema.model_fields)


@pytest.mark.asyncio
async def test_schema_cache_distinguishes_flag(db_relation_ids):
    nested_schema = pydantic_model_creator(RelationIdItem)
    flat_schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    assert nested_schema is not flat_schema
    assert nested_schema.__name__ == "tests.contrib.models_pydantic_relation_ids.RelationIdItem"
    assert flat_schema.__name__ != nested_schema.__name__
    assert "owner" in nested_schema.model_fields
    assert "owner_id" not in nested_schema.model_fields
    assert pydantic_model_creator(RelationIdItem) is nested_schema
    assert pydantic_model_creator(RelationIdItem, relations_as_ids=True) is flat_schema
    nested_excluded = pydantic_model_creator(RelationIdItem, exclude=("tags",))
    flat_excluded = pydantic_model_creator(RelationIdItem, exclude=("tags",), relations_as_ids=True)
    assert nested_excluded is not flat_excluded


@pytest.mark.asyncio
async def test_many_to_many_stays_nested(db_relation_ids):
    flat_schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    nested_schema = pydantic_model_creator(RelationIdItem)
    for schema in (flat_schema, nested_schema):
        tag_schema = schema.model_fields["tags"].annotation.__args__[0]
        assert issubclass(tag_schema, PydanticModel)
        assert {"id", "name"} <= set(tag_schema.model_fields)
    assert "owner_id" in flat_schema.model_fields["tags"].annotation.__args__[0].model_fields
    assert "owner" in nested_schema.model_fields["tags"].annotation.__args__[0].model_fields


@pytest.mark.asyncio
async def test_from_hare_orm_fills_ids_without_fetching_relations(db_relation_ids):
    await create_item()
    item = await RelationIdItem.objects.get(id=10)
    schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True, exclude=("tags",))
    async with assert_num_queries(0):
        dumped = (await schema.from_hare_orm(item)).model_dump()
    assert dumped["owner_id"] == 1
    assert dumped["reviewer_id"] == 2
    assert dumped["uuid_target_id"] == item.uuid_target_id
    assert dumped["code_target_id"] == "ABC"
    assert dumped["auditor_id"] == 1


@pytest.mark.asyncio
async def test_from_hare_orm_fetches_only_many_to_many(db_relation_ids):
    await create_item()
    item = await RelationIdItem.objects.get(id=10)
    schema = pydantic_model_creator(RelationIdItem, relations_as_ids=True)
    async with assert_num_queries(1) as counter:
        dumped = (await schema.from_hare_orm(item)).model_dump()
    # The tag table joined to the through table - never the FK targets.
    assert not any("relation_id_owner" in query for query in counter.queries)
    assert dumped["owner_id"] == 1
    assert dumped["tags"] == [{"id": 5, "name": "tag", "owner_id": 1}]


@pytest.mark.asyncio
async def test_queryset_creator_relations_as_ids(db_relation_ids):
    await create_item()
    list_schema = pydantic_queryset_creator(RelationIdItem, relations_as_ids=True, exclude=("tags",))
    submodel = list_schema.model_config["submodel"]
    assert "owner_id" in submodel.model_fields
    assert "owner" not in submodel.model_fields
    async with assert_num_queries(1):
        dumped = (await list_schema.from_queryset(RelationIdItem.objects.all())).model_dump()
    assert [row["owner_id"] for row in dumped] == [1]
    nested_list_schema = pydantic_queryset_creator(RelationIdItem, exclude=("tags",))
    assert nested_list_schema is not list_schema
