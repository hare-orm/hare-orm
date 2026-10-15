import os

import pytest
import pytest_asyncio

from hare.contrib.test.isolated_contexts import hare_test_context
from tests.fields.models_async_pk_default import AsyncPkChild, AsyncPkTarget


@pytest_asyncio.fixture
async def db_async_pk_default():
    async with hare_test_context(
        modules=["tests.fields.models_async_pk_default"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest.mark.asyncio
async def test_fk_shadow_field_does_not_inherit_target_async_default_flag(db_async_pk_default):
    assert AsyncPkTarget._meta.fields_map["id"]._default_is_coroutine
    assert not AsyncPkChild._meta.fields_map["target_id"]._default_is_coroutine


@pytest.mark.asyncio
async def test_fk_to_model_with_async_pk_default_saves_without_relation(db_async_pk_default):
    child = await AsyncPkChild.objects.create(id=1)
    assert child.target_id is None
    assert (await AsyncPkChild.objects.get(id=1)).target_id is None


@pytest.mark.asyncio
async def test_fk_to_model_with_async_pk_default_saves_with_relation(db_async_pk_default):
    target = await AsyncPkTarget.objects.create()
    child = await AsyncPkChild.objects.create(id=2, target=target)
    assert (await AsyncPkChild.objects.get(id=child.id)).target_id == target.id
