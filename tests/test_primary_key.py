from __future__ import annotations

import uuid
import warnings
from typing import Any

import pytest

from hare import fields, prefetch_related_objects
from tests.testmodels import (
    CharFkRelatedModel,
    CharM2MRelatedModel,
    CharPkModel,
    ImplicitPkModel,
    UUIDFkRelatedModel,
    UUIDM2MRelatedModel,
    UUIDPkModel,
)


class TestQueryset:
    @pytest.mark.asyncio
    async def test_implicit_pk(self, db):
        instance = await ImplicitPkModel.objects.create(value="test")
        assert instance.id
        assert instance.pk == instance.id

    @pytest.mark.asyncio
    async def test_uuid_pk(self, db):
        value = uuid.uuid4()
        await UUIDPkModel.objects.create(id=value)

        instance2 = await UUIDPkModel.objects.get(id=value)
        assert instance2.id == value
        assert instance2.pk == value

    @pytest.mark.asyncio
    async def test_uuid_pk_default(self, db):
        instance1 = await UUIDPkModel.objects.create()
        assert isinstance(instance1.id, uuid.UUID)
        assert instance1.pk == instance1.pk

        instance2 = await UUIDPkModel.objects.get(id=instance1.id)
        assert instance2.id == instance1.id
        assert instance2.pk == instance1.id

    @pytest.mark.asyncio
    async def test_uuid_pk_fk(self, db):
        value = uuid.uuid4()
        instance = await UUIDPkModel.objects.create(id=value)
        instance2 = await UUIDPkModel.objects.create(id=uuid.uuid4())
        await UUIDFkRelatedModel.objects.create(model=instance2)

        related_instance = await UUIDFkRelatedModel.objects.create(model=instance)
        assert related_instance.model_id == value

        related_instance = await UUIDFkRelatedModel.objects.filter(model=instance).first()
        assert related_instance.model_id == value

        related_instance = await UUIDFkRelatedModel.objects.filter(model_id=value).first()
        assert related_instance.model_id == value

        await prefetch_related_objects([related_instance], "model")
        assert related_instance.model == instance

        await prefetch_related_objects([instance], "children")
        assert instance.children[0] == related_instance

    @pytest.mark.asyncio
    async def test_uuid_m2m(self, db):
        value = uuid.uuid4()
        instance = await UUIDPkModel.objects.create(id=value)
        instance2 = await UUIDPkModel.objects.create(id=uuid.uuid4())

        related_instance = await UUIDM2MRelatedModel.objects.create()
        related_instance2 = await UUIDM2MRelatedModel.objects.create()

        await instance.peers.add(related_instance)
        await related_instance2.models.add(instance, instance2)

        await prefetch_related_objects([instance], "peers")
        assert len(instance.peers) == 2
        assert set(instance.peers) == {related_instance, related_instance2}

        await prefetch_related_objects([related_instance], "models")
        assert len(related_instance.models) == 1
        assert related_instance.models[0] == instance

        await prefetch_related_objects([related_instance2], "models")
        assert len(related_instance2.models) == 2
        assert {m.pk for m in related_instance2.models} == {instance.pk, instance2.pk}

        related_instance_list = await UUIDM2MRelatedModel.objects.filter(models=instance2)
        assert len(related_instance_list) == 1
        assert related_instance_list[0] == related_instance2

        related_instance_list = await UUIDM2MRelatedModel.objects.filter(models__in=[instance2])
        assert len(related_instance_list) == 1
        assert related_instance_list[0] == related_instance2

    @pytest.mark.asyncio
    async def test_char_pk(self, db):
        value = "Da-PK"
        await CharPkModel.objects.create(id=value)

        instance2 = await CharPkModel.objects.get(id=value)
        assert instance2.id == value
        assert instance2.pk == value

    @pytest.mark.asyncio
    async def test_char_pk_fk(self, db):
        value = "Da-PK-for-FK"
        instance = await CharPkModel.objects.create(id=value)
        instance2 = await CharPkModel.objects.create(id=uuid.uuid4())
        await CharFkRelatedModel.objects.create(model=instance2)

        related_instance = await CharFkRelatedModel.objects.create(model=instance)
        assert related_instance.model_id == value

        related_instance = await CharFkRelatedModel.objects.filter(model=instance).first()
        assert related_instance.model_id == value

        related_instance = await CharFkRelatedModel.objects.filter(model_id=value).first()
        assert related_instance.model_id == value

        await prefetch_related_objects([instance], "children")
        assert instance.children[0] == related_instance

    @pytest.mark.asyncio
    async def test_char_m2m(self, db):
        value = "Da-PK-for-M2M"
        instance = await CharPkModel.objects.create(id=value)
        instance2 = await CharPkModel.objects.create(id=uuid.uuid4())

        related_instance = await CharM2MRelatedModel.objects.create()
        related_instance2 = await CharM2MRelatedModel.objects.create()

        await instance.peers.add(related_instance)
        await related_instance2.models.add(instance, instance2)

        await prefetch_related_objects([related_instance], "models")
        assert len(related_instance.models) == 1
        assert related_instance.models[0] == instance

        await prefetch_related_objects([related_instance2], "models")
        assert len(related_instance2.models) == 2
        assert {m.pk for m in related_instance2.models} == {instance.pk, instance2.pk}

        related_instance_list = await CharM2MRelatedModel.objects.filter(models=instance2)
        assert len(related_instance_list) == 1
        assert related_instance_list[0] == related_instance2

        related_instance_list = await CharM2MRelatedModel.objects.filter(models__in=[instance2])
        assert len(related_instance_list) == 1
        assert related_instance_list[0] == related_instance2


# Test parameters for pk index alias tests
# Format: (Field class, init_kwargs, field_id)
PK_INDEX_ALIAS_PARAMS = [
    pytest.param(fields.CharField, {"max_length": 10}, id="CharField"),
    pytest.param(fields.UUIDField, {}, id="UUIDField"),
    pytest.param(fields.IntField, {}, id="IntField"),
    pytest.param(fields.BigIntField, {}, id="BigIntField"),
    pytest.param(fields.SmallIntField, {}, id="SmallIntField"),
]


class TestPkIndexAlias:
    """``primary_key`` has no alias - an unknown keyword is rejected rather than silently ignored."""

    @pytest.mark.parametrize("Field,init_kwargs", PK_INDEX_ALIAS_PARAMS)
    def test_pk_keyword_is_rejected(self, Field: Any, init_kwargs: dict):
        with pytest.raises(TypeError, match="unexpected keyword arguments: pk"):
            Field(pk=True, **init_kwargs)

    @pytest.mark.parametrize("Field,init_kwargs", PK_INDEX_ALIAS_PARAMS)
    def test_unknown_keyword_is_rejected(self, Field: Any, init_kwargs: dict):
        with pytest.raises(TypeError, match="unexpected keyword arguments: nul, primay_key"):
            Field(primay_key=True, nul=True, **init_kwargs)


class TestPkIndexAliasUUID:
    """A UUID primary key defaults to uuid4."""

    def test_default(self):
        f = fields.UUIDField(primary_key=True)
        assert f.default == uuid.uuid4
        f = fields.UUIDField()
        assert f.default is None
        f = fields.UUIDField(default=1)
        assert f.default == 1


# Int field types that support positional pk argument
INT_FIELD_TYPES = [
    pytest.param(fields.IntField, id="IntField"),
    pytest.param(fields.BigIntField, id="BigIntField"),
    pytest.param(fields.SmallIntField, id="SmallIntField"),
]


class TestPkIndexAliasInt:
    """Int field types support positional pk argument."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("Field", INT_FIELD_TYPES)
    async def test_argument(self, Field: Any):
        f = Field(True)
        assert f.pk is True
        f = Field(False)
        assert f.pk is False


class TestPkIndexAliasText:
    """A TextField can be the primary key."""

    def test_primary_key(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert fields.TextField(primary_key=True).pk is True
            assert fields.TextField(True).pk is True
            assert fields.TextField().pk is False
