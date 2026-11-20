import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.core.hare_context import HareContext
from tests.fields.relations.models_async_pk_default import AsyncPkChild, AsyncPkTarget
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def db_async_pk_default_module():
    async with DatabaseUnderTest.open_model_context(["tests.fields.relations.models_async_pk_default"]) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def db_async_pk_default(db_async_pk_default_module):
    async with RollbackIsolation(db_async_pk_default_module) as context:
        yield context


@pytest.mark.asyncio
async def test_fk_shadow_field_does_not_inherit_target_async_default_flag(db_async_pk_default):
    assert HareContext.require_current().apps.get_binding(AsyncPkTarget).fields_map["id"]._default_is_coroutine
    assert (
        not HareContext.require_current().apps.get_binding(AsyncPkChild).fields_map["target_id"]._default_is_coroutine
    )


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
