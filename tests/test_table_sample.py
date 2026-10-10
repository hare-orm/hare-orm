"""QuerySet.sample(): a TABLESAMPLE of the model's table under the queryset's filters, joins, counts,
aggregates and subqueries - repeatable by a seed - and the arguments, writes and databases that refuse it."""

from __future__ import annotations

import re

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import ConfigurationError, QueryError, UnSupportedError
from hare.query.enums import TableSampleMethod
from hare.query.functions import Count, Sum
from hare.query.queryset.constants import MAX_TABLE_SAMPLE_PERCENT, MAX_TABLE_SAMPLE_SEED
from hare.query.scopes.manager_scope import ManagerScope
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from tests.testmodels import Event, IntFields, OwnerScopedSecret, Tournament

ROW_COUNT = 400


async def create_rows() -> None:
    await IntFields.objects.bulk_create(
        [IntFields(id=row_id, intnum=row_id % 10) for row_id in range(1, ROW_COUNT + 1)]
    )


@requires_features(supports_table_sample=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("method", [TableSampleMethod.BERNOULLI, TableSampleMethod.SYSTEM, "bernoulli", "System"])
async def test_every_row_and_no_row(db, method):
    await create_rows()
    assert await IntFields.objects.sample(100, method=method).count() == ROW_COUNT
    assert await IntFields.objects.sample(0, method=method).count() == 0
    assert await IntFields.objects.sample(0.0, method=method).exists() is False


@requires_features(supports_table_sample=True)
@pytest.mark.asyncio
async def test_the_plan_of_the_same_query_without_a_sample_isnt_reused(db):
    await create_rows()
    # Plans kept for the queries without a sample, then the same queries with one.
    assert await IntFields.objects.count() == ROW_COUNT
    assert len(await IntFields.objects.filter(intnum=1)) == ROW_COUNT // 10
    assert len(await IntFields.objects.values_list("id", flat=True)) == ROW_COUNT
    keys = IntFields.objects.filter(intnum=1).values("id")
    assert await IntFields.objects.filter(id__in=keys).count() == ROW_COUNT // 10
    assert await IntFields.objects.sample(0).count() == 0
    assert await IntFields.objects.sample(0).filter(intnum=1) == []
    assert await IntFields.objects.sample(0).values_list("id", flat=True) == []
    assert (
        await IntFields.objects.filter(id__in=IntFields.objects.sample(0).filter(intnum=1).values("id")).count() == 0
    )
    assert await IntFields.objects.filter(
        id__in=IntFields.objects.sample(100).filter(intnum=1).values("id")
    ).count() == (ROW_COUNT // 10)


@requires_features(supports_table_sample=True)
@pytest.mark.asyncio
async def test_a_seed_repeats_the_sample(db):
    await create_rows()
    first = await IntFields.objects.sample(50, seed=7).order_by("id").values_list("id", flat=True)
    second = await IntFields.objects.sample(50, seed=7).order_by("id").values_list("id", flat=True)
    other_seed = await IntFields.objects.sample(50, seed=8).order_by("id").values_list("id", flat=True)
    assert first == second
    assert first != other_seed
    # About half the rows, the same ones in every reading of that seed.
    assert ROW_COUNT * 0.3 < len(first) < ROW_COUNT * 0.7
    assert await IntFields.objects.sample(50, seed=7).count() == len(first)
    rows = await IntFields.objects.sample(12.5, seed=MAX_TABLE_SAMPLE_SEED).values_list("id", flat=True)
    assert set(rows) <= set(range(1, ROW_COUNT + 1))


@requires_features(supports_table_sample=True)
@pytest.mark.asyncio
async def test_the_filters_apply_to_the_sample(db):
    await create_rows()
    sampled = IntFields.objects.sample(50, seed=3)
    all_sampled = set(await sampled.values_list("id", flat=True))
    filtered = await sampled.filter(intnum__lt=5).order_by("id").values_list("id", flat=True)
    assert filtered == sorted(row_id for row_id in all_sampled if row_id % 10 < 5)
    totals = await sampled.aggregate(rows=Count("id"), total=Sum("intnum"))
    assert totals == {"rows": len(all_sampled), "total": sum(row_id % 10 for row_id in all_sampled)}
    groups = await sampled.values("intnum").annotate(rows=Count("id")).order_by("intnum")
    assert sum(group["rows"] for group in groups) == len(all_sampled)
    objects = await sampled.filter(id__lte=50)
    assert {row.id for row in objects} == {row_id for row_id in all_sampled if row_id <= 50}


@requires_features(supports_table_sample=True)
@pytest.mark.asyncio
async def test_a_sample_with_joins_and_as_a_subquery(db):
    tournament = await Tournament.objects.create(id=1, name="Cup")
    for event_id in range(1, 41):
        await Event.objects.create(event_id=event_id, tournament=tournament, name=f"event {event_id}")
    events = await Event.objects.sample(100).select_related("tournament").filter(tournament__name="Cup")
    assert len(events) == 40
    assert {event.tournament.name for event in events} == {"Cup"}
    sampled_keys = Event.objects.sample(50, seed=11)
    expected = set(await sampled_keys.values_list("event_id", flat=True))
    assert set(await Event.objects.filter(event_id__in=sampled_keys).values_list("event_id", flat=True)) == expected
    # Deleting a sample - through its keys.
    deleted = await Event.objects.filter(event_id__in=Event.objects.sample(50, seed=11)).delete()
    assert deleted == len(expected)
    assert await Event.objects.count() == 40 - len(expected)


@requires_features(supports_table_sample=True)
@pytest.mark.asyncio
async def test_the_sql(db):
    sql = IntFields.objects.sample(12.5, method="system", seed=42).filter(intnum=1).sql(parameters_inline=True)
    assert 'FROM "intfields" TABLESAMPLE SYSTEM (12.5) REPEATABLE (42) WHERE' in sql
    assert "TABLESAMPLE BERNOULLI (5) WHERE" in IntFields.objects.sample(5).filter(intnum=1).sql(
        parameters_inline=True
    )


@requires_features(supports_table_sample=False)
@pytest.mark.asyncio
async def test_a_database_without_table_sample_refuses_it(db):
    await create_rows()
    async with capture_queries() as queries:
        with pytest.raises(UnSupportedError, match="sample\\(\\) needs TABLESAMPLE"):
            await IntFields.objects.sample(10).count()
        with pytest.raises(UnSupportedError, match="sample\\(\\) needs TABLESAMPLE"):
            await IntFields.objects.sample(10)
    assert queries.count == 0


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: IntFields.objects.sample(-1), "takes a percent from 0 to 100"),
        (lambda: IntFields.objects.sample(MAX_TABLE_SAMPLE_PERCENT + 0.5), "takes a percent"),
        (lambda: IntFields.objects.sample(True), "takes a percent"),
        (lambda: IntFields.objects.sample("10"), "takes a percent"),
        (lambda: IntFields.objects.sample(float("nan")), "takes a percent"),
        (lambda: IntFields.objects.sample(10**400), "takes a percent"),
        (lambda: IntFields.objects.sample(10, method="RANDOM"), "takes one of BERNOULLI, SYSTEM"),
        (lambda: IntFields.objects.sample(10, method=1), "takes one of BERNOULLI, SYSTEM"),
        (lambda: IntFields.objects.sample(10, seed=-1), "takes None or an int from 0 to"),
        (lambda: IntFields.objects.sample(10, seed=MAX_TABLE_SAMPLE_SEED + 1), "takes None or an int"),
        (lambda: IntFields.objects.sample(10, seed=1.0), "takes None or an int"),
        (lambda: IntFields.objects.sample(10, seed=False), "takes None or an int"),
        (lambda: IntFields.objects.filter(id=1).union(IntFields.objects.filter(id=2)).sample(10), "sample\\(\\)"),
    ],
)
def test_a_wrong_argument_is_refused(make, message):
    with pytest.raises(QueryError, match=message):
        make()


@pytest.mark.parametrize(
    ("make_scope_queryset", "named_call"),
    [
        (lambda: OwnerScopedSecret.objects.sample(10), "sample()"),
        (lambda: OwnerScopedSecret.objects.with_cte("picked", OwnerScopedSecret.objects.values("id")), "with_cte()"),
    ],
)
def test_a_manager_scope_with_a_sample_is_refused_as_a_join_condition(monkeypatch, make_scope_queryset, named_call):
    monkeypatch.setattr(RowScopes, "get_queryset", staticmethod(lambda model, visibility=None: make_scope_queryset()))
    with pytest.raises(ConfigurationError, match=f"applies {re.escape(named_call)}, which can't be expressed"):
        ManagerScope(OwnerScopedSecret).get_condition(RowVisibility.DEFAULT)


@pytest.mark.parametrize(
    ("write", "method_name"),
    [
        (lambda queryset: queryset.update(intnum=1), "update"),
        (lambda queryset: queryset.delete(), "delete"),
        (lambda queryset: queryset.hard_delete(), "hard_delete"),
    ],
)
def test_a_write_on_a_sample_is_refused(write, method_name):
    with pytest.raises(QueryError, match=f"{method_name}\\(\\) can't be used on a sample\\(\\)"):
        write(IntFields.objects.sample(10))
