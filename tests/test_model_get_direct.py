"""Model.objects.get() and Model.objects.get_or_none() of a queryset nothing else changed run on
their shape's statement plan directly - the filters' values are bound, no query is built."""

from unittest.mock import patch

import pytest

from hare.contrib.test import requires_features
from hare.core.caches import Caches
from hare.dialects.base.client import TransactionClient
from hare.exceptions import (
    DoesNotExist,
    FieldError,
    MultipleObjectsReturned,
    QueryError,
    UnSupportedError,
)
from hare.query.expressions import F, Q
from hare.query.plans.statement_plans import StatementPlans
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.transactions.transactions import Transactions
from tests import testmodels


class NotFoundError(Exception):
    pass


def spy_on_queryset_init():
    """Spies on building the query a queryset runs - a direct get() builds none."""
    return patch.object(ModelRowsQuery, "__init__", autospec=True, side_effect=ModelRowsQuery.__init__)


def get_direct_plan(query):
    """The plan a direct get() runs on - None when it is built as usual."""
    return query._direct_get.plan if query._direct_get is not None else None


def is_direct_get(query) -> bool:
    return query._direct_get is not None


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_get_by_pk_builds_no_queryset(db):
    obj = await testmodels.IntFields.objects.create(intnum=5)
    # The first query of the shape runs its QuerySet, which records the plan.
    await testmodels.IntFields.objects.get(pk=obj.id)
    with spy_on_queryset_init() as queryset_init:
        query = testmodels.IntFields.objects.get(pk=obj.id)
        fetched = await query
    assert is_direct_get(query)
    assert get_direct_plan(query) is not None
    assert not queryset_init.called
    assert fetched.id == obj.id
    assert fetched.intnum == 5
    assert fetched._saved_in_db


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_first_query_of_a_shape_runs_its_queryset_and_keeps_the_plan(db):
    obj = await testmodels.IntFields.objects.create(intnum=31)
    StatementPlans.forget_model(testmodels.IntFields)
    first = testmodels.IntFields.objects.get(intnum=31)
    assert is_direct_get(first)
    assert get_direct_plan(first) is None
    assert (await first).id == obj.id
    second = testmodels.IntFields.objects.get(intnum=31)
    assert get_direct_plan(second) is not None
    # The very plan the query built as usual runs on.
    query = testmodels.IntFields.objects.filter(intnum=31)
    query = query._get_single_queryset((), {}, exception=None, raise_does_not_exist=True)._get_model_rows_query()
    query._make_query_to_run()
    assert query._statement_plan is get_direct_plan(second)


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_plan_hit_is_counted(db):
    obj = await testmodels.IntFields.objects.create(intnum=32)
    await testmodels.IntFields.objects.get(pk=obj.id)
    hits = StatementPlans.hits
    await testmodels.IntFields.objects.get(pk=obj.id)
    assert StatementPlans.hits == hits + 1


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_get_by_field_and_column_names(db):
    obj = await testmodels.SourceFields.objects.create(chars="direct")
    assert is_direct_get(testmodels.SourceFields.objects.get(eyedee=obj.eyedee))
    for _ in range(2):
        assert (await testmodels.SourceFields.objects.get(eyedee=obj.eyedee)).pk == obj.pk
        assert (await testmodels.SourceFields.objects.get(pk=obj.pk, chars="direct")).pk == obj.pk


@pytest.mark.asyncio
async def test_no_match_raises_does_not_exist_or_the_given_exception(db):
    for _ in range(2):
        with pytest.raises(DoesNotExist):
            await testmodels.IntFields.objects.get(pk=10**9)
        with pytest.raises(NotFoundError):
            await testmodels.IntFields.objects.get(pk=10**9, exception=NotFoundError)
        instance = NotFoundError("gone")
        with pytest.raises(NotFoundError) as raised:
            await testmodels.IntFields.objects.get(pk=10**9, exception=instance)
        assert raised.value is instance


@pytest.mark.asyncio
async def test_several_matches_raise_multiple_objects_returned(db):
    await testmodels.IntFields.objects.create(intnum=77)
    await testmodels.IntFields.objects.create(intnum=77)
    for _ in range(2):
        with pytest.raises(MultipleObjectsReturned):
            await testmodels.IntFields.objects.get(intnum=77)


@pytest.mark.asyncio
async def test_get_or_none(db):
    same = [await testmodels.Author.objects.create(name="same") for _ in range(2)]
    other = await testmodels.Author.objects.create(name="other")
    for _ in range(2):
        assert await testmodels.Author.objects.get_or_none(name="other") == other
        assert await testmodels.Author.objects.get_or_none(name="nobody") is None
        with pytest.raises(MultipleObjectsReturned):
            await testmodels.Author.objects.get_or_none(name="same")
    assert len(same) == 2


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_get_and_get_or_none_share_a_plan(db):
    other = await testmodels.Author.objects.create(name="other")
    StatementPlans.forget_model(testmodels.Author)
    assert await testmodels.Author.objects.get(name="other") == other
    query = testmodels.Author.objects.get_or_none(name="other")
    assert is_direct_get(query)
    assert get_direct_plan(query) is not None
    assert await query == other


@pytest.mark.asyncio
async def test_get_or_none_chained_goes_to_its_queryset(db):
    other = await testmodels.Author.objects.create(name="other")
    assert await testmodels.Author.objects.get_or_none(name="other").values_list("id", flat=True) == other.id
    assert await testmodels.Author.objects.get_or_none(name="nobody").values_list("id", flat=True) is None
    assert await testmodels.Author.objects.get_or_none(name="other").only("id") == other


@pytest.mark.asyncio
async def test_chained_calls_go_to_the_queryset(db):
    obj = await testmodels.IntFields.objects.create(intnum=3)
    for _ in range(2):
        query = testmodels.IntFields.objects.get(pk=obj.id)
        assert await query.values("intnum") == {"intnum": 3}
        assert await query.values_list("intnum", flat=True) == 3
        partial = await query.only("id", "intnum")
        assert partial._partial and partial.intnum == 3
        assert "WHERE" in query.sql()
        # The direct query itself still runs after the queryset was built.
        assert (await query).id == obj.id


@pytest.mark.asyncio
async def test_query_can_be_awaited_again(db):
    obj = await testmodels.IntFields.objects.create(intnum=1)
    query = testmodels.IntFields.objects.get(pk=obj.id)
    first = await query
    await testmodels.IntFields.objects.filter(id=obj.id).update(intnum=2)
    second = await query
    assert first.intnum == 1
    assert second.intnum == 2


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_other_lookups_run_on_their_plan_too(db):
    obj = await testmodels.IntFields.objects.create(intnum=9)
    shapes = [
        {"intnum__gt": 8},
        {"intnum__gte": 9, "id": obj.id},
        {"intnum__in": [9, 10]},
        {"intnum__range": (9, 9)},
    ]
    for kwargs in shapes:
        assert (await testmodels.IntFields.objects.get(**kwargs)).id == obj.id
        with spy_on_queryset_init() as queryset_init:
            query = testmodels.IntFields.objects.get(**kwargs)
            assert (await query).id == obj.id
        assert get_direct_plan(query) is not None, kwargs
        assert not queryset_init.called, kwargs


@pytest.mark.asyncio
async def test_each_value_is_bound(db):
    first = await testmodels.IntFields.objects.create(intnum=101)
    second = await testmodels.IntFields.objects.create(intnum=102)
    for _ in range(2):
        assert (await testmodels.IntFields.objects.get(intnum=101)).id == first.id
        assert (await testmodels.IntFields.objects.get(intnum=102)).id == second.id
        assert (await testmodels.IntFields.objects.get(intnum__in=[101, 7])).id == first.id
        assert (await testmodels.IntFields.objects.get(intnum__in=[7, 102])).id == second.id


@pytest.mark.asyncio
async def test_value_shapes_without_a_plan_run_their_queryset(db):
    obj = await testmodels.IntFields.objects.create(intnum=9, intnum_null=None)
    for _ in range(2):
        for kwargs in ({"intnum": F("intnum"), "id": obj.id}, {"intnum_null": None, "id": obj.id}):
            query = testmodels.IntFields.objects.get(**kwargs)
            assert is_direct_get(query)
            assert get_direct_plan(query) is None
            assert (await query).id == obj.id


@pytest.mark.asyncio
async def test_a_value_the_plan_cannot_bind_runs_the_queryset(db):
    """The same filter keys and value types, a list of another length: another shape."""
    obj = await testmodels.IntFields.objects.create(intnum=9)
    assert (await testmodels.IntFields.objects.get(intnum__in=[9, 10])).id == obj.id
    assert (await testmodels.IntFields.objects.get(intnum__in=[9, 10, 11])).id == obj.id
    assert (await testmodels.IntFields.objects.get(intnum__in=[8, 9])).id == obj.id
    # A generator is read once, into the list the QuerySet filters by.
    assert (await testmodels.IntFields.objects.get(intnum__in=(value for value in (9, 10)))).id == obj.id
    assert (await testmodels.IntFields.objects.get(intnum__in=(value for value in (9, 10)))).id == obj.id


@pytest.mark.asyncio
async def test_conditions_build_a_queryset(db):
    obj = await testmodels.IntFields.objects.create(intnum=9)
    assert not is_direct_get(testmodels.IntFields.objects.get(Q(pk=obj.id)))
    assert (await testmodels.IntFields.objects.get(Q(pk=obj.id))).id == obj.id


@pytest.mark.asyncio
async def test_rejected_filters_are_rejected_when_get_is_called(db):
    """A filter the QuerySet rejects as it is added is rejected by get() itself, not when the
    query is awaited - and an unknown field when it runs, as on any QuerySet built by get()."""
    with pytest.raises(UnSupportedError):
        testmodels.IntFields.objects.get(intnum__in="12")
    with pytest.raises(QueryError):
        testmodels.CompositePkThing.objects.get(pk=(1,))
    with pytest.raises(FieldError):
        await testmodels.IntFields.objects.get(no_such_field=1)
    with pytest.raises(FieldError):
        await testmodels.IntFields.objects.get(no_such_field=1)


@pytest.mark.asyncio
async def test_composite_primary_key(db):
    thing = await testmodels.CompositePkThing.objects.create(thing_id=1, revision=2, name="composite")
    for _ in range(2):
        query = testmodels.CompositePkThing.objects.get(pk=(1, 2))
        assert (await query).name == thing.name
    # pk= is rewritten into one filter per key column - other values than the query was given.
    assert get_direct_plan(testmodels.CompositePkThing.objects.get(pk=(1, 2))) is None
    for _ in range(2):
        assert (await testmodels.CompositePkThing.objects.get(thing_id=1, revision=2)).name == thing.name


@pytest.mark.asyncio
async def test_models_with_default_behaviour_take_the_queryset(db):
    assert not is_direct_get(testmodels.ManagerModel.objects.get(pk=1))
    assert not is_direct_get(testmodels.LazyJoinedChild.objects.get(pk=1))
    assert not is_direct_get(testmodels.LazySelectChild.objects.get(pk=1))


@pytest.mark.asyncio
async def test_soft_deleted_row_stays_hidden(db):
    obj = await testmodels.SoftDeleteStandalone.objects.create(name="hidden")
    await obj.delete()
    with pytest.raises(DoesNotExist):
        await testmodels.SoftDeleteStandalone.objects.get(pk=obj.pk)


@pytest.mark.asyncio
async def test_connection_is_chosen_when_get_is_called(db):
    obj = await testmodels.IntFields.objects.create(intnum=4)
    connection = testmodels.IntFields.get_connection()
    query = testmodels.IntFields.objects.using(connection).get(pk=obj.id)
    assert query._direct_get.db is connection


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_get_inside_a_transaction_runs_on_it(db):
    async with Transactions.atomic():
        created = await testmodels.IntFields.objects.create(intnum=8)
        for _ in range(2):
            inside = testmodels.IntFields.objects.get(pk=created.id)
            assert isinstance(inside._direct_get.db, TransactionClient)
            assert (await inside).intnum == 8


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_forgetting_model_caches_drops_the_plan(db):
    obj = await testmodels.IntFields.objects.create(intnum=6)
    await testmodels.IntFields.objects.get(pk=obj.id)
    assert get_direct_plan(testmodels.IntFields.objects.get(pk=obj.id)) is not None
    Caches.forget_model_caches([testmodels.IntFields])
    assert get_direct_plan(testmodels.IntFields.objects.get(pk=obj.id)) is None
    assert (await testmodels.IntFields.objects.get(pk=obj.id)).id == obj.id


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_get_or_create_reads_without_a_queryset(db):
    obj = await testmodels.IntFields.objects.create(intnum=11)
    await testmodels.IntFields.objects.get(id=obj.id)
    with spy_on_queryset_init() as queryset_init:
        fetched, created = await testmodels.IntFields.objects.get_or_create(id=obj.id)
    assert not created
    assert fetched.intnum == 11
    assert not queryset_init.called
    created_row, created = await testmodels.IntFields.objects.get_or_create(intnum=12, defaults={"intnum_null": 1})
    assert created
    assert (await testmodels.IntFields.objects.get_or_create(intnum=12))[0].id == created_row.id


@pytest.mark.asyncio
async def test_fetched_instance_flags(db):
    obj = await testmodels.IntFields.objects.create(intnum=1)
    for _ in range(2):
        fetched = await testmodels.IntFields.objects.get(pk=obj.id)
        assert fetched._saved_in_db is True
        assert fetched._partial is False
        assert fetched._custom_generated_pk is False
        assert fetched._await_when_save == {}
    tracked = await testmodels.DirtyTrackedThing.objects.create(name="a", count=1)
    for _ in range(2):
        assert (await testmodels.DirtyTrackedThing.objects.get(pk=tracked.id)).get_dirty_fields() == {}
