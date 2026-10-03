import pytest

from hare import prefetch_related_objects
from hare.exceptions import (
    IntegrityError,
    NoValuesFetched,
    QueryError,
)
from hare.query.queryset import QuerySet
from tests import testmodels


# Parameterize to test both standard and source-field models
@pytest.fixture(
    params=[
        pytest.param(
            (
                testmodels.UUIDPkModel,
                testmodels.UUIDFkRelatedModel,
                testmodels.UUIDFkRelatedNullModel,
            ),
            id="standard",
        ),
        pytest.param(
            (
                testmodels.UUIDPkSourceModel,
                testmodels.UUIDFkRelatedSourceModel,
                testmodels.UUIDFkRelatedNullSourceModel,
            ),
            id="sourced",
        ),
    ]
)
def uuid_models(request):
    """
    Fixture providing UUID model classes for FK tests.

    Tests both standard UUID models and source-field models:
    * UUID needs escaping, so a good indicator of where we may have missed it.
    * UUID populates a value BEFORE it gets committed to DB, whereas int is AFTER.
    * UUID is stored differently for different DB backends. (native in PG)

    The sourced variant tests identical Python-like models with customized DB names,
    helping ensure we don't confuse the two concepts.
    """
    return request.param


@pytest.mark.asyncio
async def test_empty(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    with pytest.raises(IntegrityError):
        await UUIDFkRelatedModel.objects.create()


@pytest.mark.asyncio
async def test_empty_null(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    relation = await UUIDFkRelatedNullModel.objects.create()
    assert (await UUIDFkRelatedNullModel.objects.get(id=relation.id)).model_id is None


@pytest.mark.asyncio
async def test_create_by_id(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model_id=tour.id)
    assert rel.model_id == tour.id
    assert (await tour.children.all())[0] == rel


@pytest.mark.asyncio
async def test_create_by_name(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([rel], "model")
    assert rel.model == tour
    assert (await tour.children.all())[0] == rel


@pytest.mark.asyncio
async def test_by_name__created_prefetched(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    assert rel.model == tour
    assert (await tour.children.all())[0] == rel


@pytest.mark.asyncio
async def test_by_name__unfetched(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    rel = await UUIDFkRelatedModel.objects.get(id=rel.id)
    assert isinstance(rel.model, QuerySet)


@pytest.mark.asyncio
async def test_by_name__re_awaited(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([rel], "model")
    assert rel.model == tour
    assert await rel.model == tour


@pytest.mark.asyncio
async def test_by_name__awaited(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    rel = await UUIDFkRelatedModel.objects.get(id=rel.id)
    assert await rel.model == tour
    assert (await tour.children.all())[0] == rel


@pytest.mark.asyncio
async def test_update_by_name(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    tour2 = await UUIDPkModel.objects.create()
    rel0 = await UUIDFkRelatedModel.objects.create(model=tour)

    await UUIDFkRelatedModel.objects.filter(id=rel0.id).update(model=tour2)
    rel = await UUIDFkRelatedModel.objects.get(id=rel0.id)

    await prefetch_related_objects([rel], "model")
    assert rel.model == tour2
    assert await tour.children.all() == []
    assert (await tour2.children.all())[0] == rel


@pytest.mark.asyncio
async def test_update_by_id(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    tour2 = await UUIDPkModel.objects.create()
    rel0 = await UUIDFkRelatedModel.objects.create(model_id=tour.id)

    await UUIDFkRelatedModel.objects.filter(id=rel0.id).update(model_id=tour2.id)
    rel = await UUIDFkRelatedModel.objects.get(id=rel0.id)

    assert rel.model_id == tour2.id
    assert await tour.children.all() == []
    assert (await tour2.children.all())[0] == rel


@pytest.mark.asyncio
async def test_uninstantiated_create(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = UUIDPkModel()
    with pytest.raises(QueryError, match="You should first call .save()"):
        await UUIDFkRelatedModel.objects.create(model=tour)


@pytest.mark.asyncio
async def test_uninstantiated_iterate(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = UUIDPkModel()
    with pytest.raises(QueryError, match="This objects hasn't been instanced, call .save()"):
        async for _ in tour.children:
            pass


@pytest.mark.asyncio
async def test_uninstantiated_await(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = UUIDPkModel()
    with pytest.raises(QueryError, match="This objects hasn't been instanced, call .save()"):
        await tour.children


@pytest.mark.asyncio
async def test_unfetched_contains(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        "a" in tour.children  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_unfetched_iter(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        for _ in tour.children:
            pass


@pytest.mark.asyncio
async def test_unfetched_len(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        len(tour.children)


@pytest.mark.asyncio
async def test_unfetched_bool(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        bool(tour.children)


@pytest.mark.asyncio
async def test_unfetched_getitem(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        tour.children[0]  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_instantiated_create(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    relation = await UUIDFkRelatedModel.objects.create(model=tour)
    assert (await UUIDFkRelatedModel.objects.get(id=relation.id)).model_id == tour.id


@pytest.mark.asyncio
async def test_instantiated_iterate(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    relation = await UUIDFkRelatedModel.objects.create(model=tour)
    assert [item async for item in tour.children] == [relation]


@pytest.mark.asyncio
async def test_instantiated_await(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    relation = await UUIDFkRelatedModel.objects.create(model=tour)
    assert list(await tour.children) == [relation]


@pytest.mark.asyncio
async def test_minimal__fetched_contains(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([tour], "children")
    assert rel in tour.children


@pytest.mark.asyncio
async def test_minimal__fetched_iter(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([tour], "children")
    assert list(tour.children) == [rel]


@pytest.mark.asyncio
async def test_minimal__fetched_len(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([tour], "children")
    assert len(tour.children) == 1


@pytest.mark.asyncio
async def test_minimal__fetched_bool(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    await prefetch_related_objects([tour], "children")
    assert not bool(tour.children)
    await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([tour], "children")
    assert bool(tour.children)


@pytest.mark.asyncio
async def test_minimal__fetched_getitem(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    rel = await UUIDFkRelatedModel.objects.create(model=tour)
    await prefetch_related_objects([tour], "children")
    assert tour.children[0] == rel

    with pytest.raises(IndexError):
        tour.children[1]  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_event__filter(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event1 = await UUIDFkRelatedModel.objects.create(name="Event1", model=tour)
    event2 = await UUIDFkRelatedModel.objects.create(name="Event2", model=tour)
    assert await tour.children.filter(name="Event1") == [event1]
    assert await tour.children.filter(name="Event2") == [event2]
    assert await tour.children.filter(name="Event3") == []


@pytest.mark.asyncio
async def test_event__all(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event1 = await UUIDFkRelatedModel.objects.create(name="Event1", model=tour)
    event2 = await UUIDFkRelatedModel.objects.create(name="Event2", model=tour)
    assert set(await tour.children.all()) == {event1, event2}


@pytest.mark.asyncio
async def test_event__order_by(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event1 = await UUIDFkRelatedModel.objects.create(name="Event1", model=tour)
    event2 = await UUIDFkRelatedModel.objects.create(name="Event2", model=tour)
    assert await tour.children.order_by("-name") == [event2, event1]
    assert await tour.children.order_by("name") == [event1, event2]


@pytest.mark.asyncio
async def test_event__limit(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event1 = await UUIDFkRelatedModel.objects.create(name="Event1", model=tour)
    event2 = await UUIDFkRelatedModel.objects.create(name="Event2", model=tour)
    await UUIDFkRelatedModel.objects.create(name="Event3", model=tour)
    assert await tour.children.order_by("name").limit(2) == [event1, event2]


@pytest.mark.asyncio
async def test_event__offset(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    await UUIDFkRelatedModel.objects.create(name="Event1", model=tour)
    event2 = await UUIDFkRelatedModel.objects.create(name="Event2", model=tour)
    event3 = await UUIDFkRelatedModel.objects.create(name="Event3", model=tour)
    assert await tour.children.order_by("name").offset(1) == [event2, event3]


@pytest.mark.asyncio
async def test_assign_by_id(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedNullModel.objects.create(model=None)
    event.model_id = tour.id
    await event.save()
    event0 = await UUIDFkRelatedNullModel.objects.get(id=event.id)
    assert event0.model_id == tour.id
    await prefetch_related_objects([event0], "model")
    assert event0.model == tour


@pytest.mark.asyncio
async def test_assign_by_name(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedNullModel.objects.create(model=None)
    event.model = tour
    await event.save()
    event0 = await UUIDFkRelatedNullModel.objects.get(id=event.id)
    assert event0.model_id == tour.id
    await prefetch_related_objects([event0], "model")
    assert event0.model == tour


@pytest.mark.asyncio
async def test_assign_none_by_id(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedNullModel.objects.create(model=tour)
    event.model_id = None
    await event.save()
    event0 = await UUIDFkRelatedNullModel.objects.get(id=event.id)
    assert event0.model_id is None
    await prefetch_related_objects([event0], "model")
    assert event0.model is None


@pytest.mark.asyncio
async def test_assign_none_by_name(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedNullModel.objects.create(model=tour)
    event.model = None
    await event.save()
    event0 = await UUIDFkRelatedNullModel.objects.get(id=event.id)
    assert event0.model_id is None
    await prefetch_related_objects([event0], "model")
    assert event0.model is None


@pytest.mark.asyncio
async def test_assign_none_by_id_fails(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedModel.objects.create(model=tour)
    event.model_id = None
    with pytest.raises(IntegrityError):
        await event.save()


@pytest.mark.asyncio
async def test_assign_none_by_name_fails(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedModel.objects.create(model=tour)
    event.model = None
    with pytest.raises(IntegrityError):
        await event.save()


@pytest.mark.asyncio
async def test_delete_by_name(db, uuid_models):
    UUIDPkModel, UUIDFkRelatedModel, UUIDFkRelatedNullModel = uuid_models
    tour = await UUIDPkModel.objects.create()
    event = await UUIDFkRelatedModel.objects.create(model=tour)
    del event.model
    with pytest.raises(IntegrityError):
        await event.save()
