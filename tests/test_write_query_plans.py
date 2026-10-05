"""``update()`` and ``delete()`` keep the plan of their whole statement: a later statement of the same
shape binds its own filter values, assigned values and ``auto_now`` moment into the plan's SQL.
Each test runs its statements at least twice, so both the build and the plan hit write the rows."""

import datetime
from decimal import Decimal

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import (
    FieldError,
    QueryError,
    ValidationError,
)
from hare.query.expressions import F, Q, RawSQL
from hare.query.functions import Count
from hare.query.plans.statement.statement_plans import StatementPlans
from tests import testmodels
from tests.testmodels import DatetimeFields, DecimalFields, Event, IntFields, Reporter, Tournament, VersionedThing


async def count_plan_hits(statement) -> tuple[int, int]:
    """Awaits ``statement``.

    Returns:
        Its result, and how many statements ran on a found plan meanwhile.
    """
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


@pytest.mark.asyncio
async def test_update_binds_its_filters_and_values(db):
    first = await IntFields.objects.create(intnum=1)
    second = await IntFields.objects.create(intnum=2)
    assert await IntFields.objects.filter(id=first.id).update(intnum=10) == 1
    updated, hits = await count_plan_hits(IntFields.objects.filter(id=second.id).update(intnum=20))
    assert updated == 1
    assert hits == 1
    assert (await IntFields.objects.get(id=first.id)).intnum == 10
    assert (await IntFields.objects.get(id=second.id)).intnum == 20


@pytest.mark.asyncio
async def test_update_with_an_expression_binds_its_literals(db):
    obj = await testmodels.FloatFields.objects.create(id=1, floatnum=1.0)
    await testmodels.FloatFields.objects.filter(id=obj.id).update(floatnum=F("floatnum") + 1.5)
    _, hits = await count_plan_hits(
        testmodels.FloatFields.objects.filter(id=obj.id).update(floatnum=F("floatnum") + 5.5)
    )
    assert hits == 1
    assert (await testmodels.FloatFields.objects.get(id=obj.id)).floatnum == 8.0
    # Another operator is another shape.
    _, hits = await count_plan_hits(
        testmodels.FloatFields.objects.filter(id=obj.id).update(floatnum=F("floatnum") * 2.0)
    )
    assert hits == 0
    assert (await testmodels.FloatFields.objects.get(id=obj.id)).floatnum == 16.0


@pytest.mark.asyncio
async def test_update_of_an_int_with_an_expression(db):
    """On a dialect without integer column ranges the result is re-checked from RETURNING rows
    - the statement is then built each time; the values are right either way."""
    obj = await IntFields.objects.create(intnum=1)
    for step, expected in ((1, 2), (5, 7), (3, 10)):
        await IntFields.objects.filter(id=obj.id).update(intnum=F("intnum") + step)
        assert (await IntFields.objects.get(id=obj.id)).intnum == expected


@pytest.mark.asyncio
async def test_update_sets_every_auto_now_field_to_its_own_moment(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    moments = []
    for name in ("first", "second", "third"):
        await Event.objects.filter(event_id=event.event_id).update(name=name)
        fetched = await Event.objects.get(event_id=event.event_id)
        assert fetched.name == name
        moments.append(fetched.modified)
    assert moments[0] < moments[1] < moments[2]


@pytest.mark.asyncio
async def test_update_of_datetime_auto_now_and_explicit_values(db):
    obj = await DatetimeFields.objects.create(id=1, datetime=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC))
    for day in (2, 3):
        moment = datetime.datetime(2024, 1, day, 12, tzinfo=datetime.UTC)
        await DatetimeFields.objects.filter(id=obj.id).update(datetime=moment)
        assert (await DatetimeFields.objects.get(id=obj.id)).datetime == moment


@pytest.mark.asyncio
async def test_update_rounds_like_a_write(db):
    """An assigned value goes through the write conversion - a DecimalField value is rounded to
    its decimal places on the plan hit too, unlike a filter value."""
    obj = await DecimalFields.objects.create(decimal=Decimal(0), decimal_nodec=Decimal(0))
    for value, stored in ((Decimal("1.4"), Decimal(1)), (Decimal("2.6"), Decimal(3))):
        await DecimalFields.objects.filter(id=obj.id).update(decimal_nodec=value)
        assert (await DecimalFields.objects.get(id=obj.id)).decimal_nodec == stored


@pytest.mark.asyncio
async def test_update_to_none_and_back(db):
    obj = await IntFields.objects.create(intnum=1, intnum_null=5)
    for _ in range(2):
        await IntFields.objects.filter(id=obj.id).update(intnum_null=None)
        assert (await IntFields.objects.get(id=obj.id)).intnum_null is None
        await IntFields.objects.filter(id=obj.id).update(intnum_null=7)
        assert (await IntFields.objects.get(id=obj.id)).intnum_null == 7


@pytest.mark.asyncio
async def test_update_of_a_relation(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    reporters = [await Reporter.objects.create(name=f"r{index}") for index in range(3)]
    for reporter in reporters:
        await Event.objects.filter(event_id=event.event_id).update(reporter=reporter)
        assert (await Event.objects.get(event_id=event.event_id)).reporter_id == reporter.id
    await Event.objects.filter(event_id=event.event_id).update(reporter=None)
    assert (await Event.objects.get(event_id=event.event_id)).reporter_id is None


@pytest.mark.asyncio
async def test_update_rejects_what_the_build_rejects_on_a_plan_hit_too(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    reporter = await Reporter.objects.create(name="r")
    for _ in range(2):
        await Event.objects.filter(event_id=event.event_id).update(reporter=reporter)
    with pytest.raises(QueryError):
        await Event.objects.filter(event_id=event.event_id).update(reporter=Reporter(name="unsaved"))
    with pytest.raises(ValidationError):
        await Event.objects.filter(event_id=event.event_id).update(reporter=tournament)
    for _ in range(2):
        with pytest.raises(FieldError):
            await IntFields.objects.filter(id=1).update(no_such_field=1)
        with pytest.raises(QueryError, match="is PK and can not be updated"):
            await IntFields.objects.filter(id=1).update(id=2)


@pytest.mark.asyncio
async def test_update_bumps_the_optimistic_lock_field(db):
    thing = await VersionedThing.objects.create(name="a")
    for index, name in enumerate(("b", "c", "d"), start=1):
        await VersionedThing.objects.filter(id=thing.id).update(name=name)
        fetched = await VersionedThing.objects.get(id=thing.id)
        assert (fetched.name, fetched.version) == (name, thing.version + index)


@pytest.mark.asyncio
async def test_update_value_types_keep_separate_plans(db):
    obj = await IntFields.objects.create(intnum=1)
    await IntFields.objects.filter(id=obj.id).update(intnum=2)
    # A whole float is written as an int, by another plan than the int's.
    _, hits = await count_plan_hits(IntFields.objects.filter(id=obj.id).update(intnum=3.0))
    assert hits == 0
    assert (await IntFields.objects.get(id=obj.id)).intnum == 3


@pytest.mark.asyncio
async def test_update_with_other_filters(db):
    first = await IntFields.objects.create(intnum=1)
    second = await IntFields.objects.create(intnum=2)
    for _ in range(2):
        assert await IntFields.objects.filter(Q(id=first.id) | Q(id=second.id)).update(intnum_null=1) == 2
        assert await IntFields.objects.filter(intnum__in=[1, 2]).update(intnum_null=2) == 2
        assert await IntFields.objects.exclude(id=first.id).update(intnum_null=3) == 1
        assert await IntFields.objects.filter(intnum__gte=2).order_by("id").limit(1).update(intnum_null=4) == 1
    assert (await IntFields.objects.get(id=first.id)).intnum_null == 2
    assert (await IntFields.objects.get(id=second.id)).intnum_null == 4


@pytest.mark.asyncio
async def test_update_through_a_relation_filter(db):
    tournament = await Tournament.objects.create(name="t")
    other = await Tournament.objects.create(name="other")
    event = await Event.objects.create(name="e", tournament=tournament)
    await Event.objects.create(name="f", tournament=other)
    for name in ("x", "y"):
        assert await Event.objects.filter(tournament__name="t").update(name=name) == 1
        assert (await Event.objects.get(event_id=event.event_id)).name == name


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_update_revalidated_from_its_returning_rows(db):
    """An expression assigned to a field with validators is re-checked from the statement's own
    RETURNING rows - such a statement is built each time."""
    obj = await testmodels.ValidatorModel.objects.create(max_value=1)
    for _ in range(2):
        await testmodels.ValidatorModel.objects.filter(id=obj.id).update(max_value=F("max_value") + 1)
    with pytest.raises(ValidationError):
        await testmodels.ValidatorModel.objects.filter(id=obj.id).update(max_value=F("max_value") + 10**6)


@pytest.mark.asyncio
async def test_delete_binds_its_filters(db):
    rows = [await IntFields.objects.create(intnum=index) for index in range(4)]
    assert await IntFields.objects.filter(intnum=0).delete() == 1
    deleted, hits = await count_plan_hits(IntFields.objects.filter(intnum=1).delete())
    assert deleted == 1
    assert hits == 1
    assert sorted(row.intnum for row in await IntFields.objects.all()) == [2, 3]
    for _ in range(2):
        assert await IntFields.objects.filter(id__in=[rows[2].id, rows[3].id]).delete() in (0, 2)
    assert await IntFields.objects.all() == []


@pytest.mark.asyncio
async def test_delete_through_a_relation_filter(db):
    first = await Tournament.objects.create(name="a")
    second = await Tournament.objects.create(name="b")
    await Event.objects.create(name="e", tournament=first)
    await Event.objects.create(name="f", tournament=second)
    for name in ("a", "b"):
        assert await Event.objects.filter(tournament__name=name).delete() == 1
    assert await Event.objects.all() == []


async def create_numbered_rows() -> list[IntFields]:
    return [await IntFields.objects.create(intnum=number) for number in range(1, 7)]


@pytest.mark.asyncio
async def test_update_of_an_ordered_slice_runs_on_its_plan(db):
    """The LIMIT is bound per statement - inline on SQLite, in the matching-rows subquery on
    PostgreSQL."""
    rows = await create_numbered_rows()
    await IntFields.objects.filter(intnum__gte=1).order_by("intnum").limit(2).update(intnum_null=10)
    updated, hits = await count_plan_hits(
        IntFields.objects.filter(intnum__gte=3).order_by("intnum").limit(3).update(intnum_null=20)
    )
    assert (updated, hits) == (3, 1)
    values = {row.intnum: row.intnum_null for row in await IntFields.objects.filter(id__in=[row.id for row in rows])}
    assert values == {1: 10, 2: 10, 3: 20, 4: 20, 5: 20, 6: None}


@pytest.mark.asyncio
async def test_update_and_delete_past_an_offset_run_on_their_plan(db):
    await create_numbered_rows()
    await IntFields.objects.filter(intnum__gte=1).order_by("intnum").offset(4).update(intnum_null=1)
    updated, hits = await count_plan_hits(
        IntFields.objects.filter(intnum__gte=2).order_by("intnum").offset(3).update(intnum_null=2)
    )
    assert (updated, hits) == (2, 1)
    assert sorted(await IntFields.objects.filter(intnum_null=2).values_list("intnum", flat=True)) == [5, 6]
    await IntFields.objects.filter(intnum__gte=5).order_by("intnum").offset(1).delete()
    deleted, hits = await count_plan_hits(
        IntFields.objects.filter(intnum__gte=1).order_by("intnum").offset(3).delete()
    )
    assert (deleted, hits) == (2, 1)
    assert sorted(await IntFields.objects.all().values_list("intnum", flat=True)) == [1, 2, 3]


@pytest.mark.asyncio
async def test_update_and_delete_filtered_across_a_relation_run_on_their_plan(db):
    tournaments = [await Tournament.objects.create(name=name) for name in ("a", "b", "c")]
    for tournament in tournaments:
        await Event.objects.create(name=f"{tournament.name}-event", tournament=tournament)
    await Event.objects.filter(tournament__name="a").update(name="first")
    updated, hits = await count_plan_hits(Event.objects.filter(tournament__name__in=["b", "c"]).update(name="second"))
    assert (updated, hits) == (2, 0)
    updated, hits = await count_plan_hits(Event.objects.filter(tournament__name__in=["a", "b"]).update(name="third"))
    assert (updated, hits) == (2, 1)
    assert sorted(await Event.objects.all().values_list("name", flat=True)) == ["second", "third", "third"]
    await Event.objects.filter(tournament__name="c").delete()
    deleted, hits = await count_plan_hits(Event.objects.filter(tournament__name="a").delete())
    # A dialect may run statements of its own around a delete (reading the keys first) - each
    # on its plan too.
    assert deleted == 1
    assert hits >= 1
    assert await Event.objects.all().values_list("name", flat=True) == ["third"]


@pytest.mark.asyncio
async def test_update_filtered_on_an_aggregate_annotation_runs_on_its_plan(db):
    tournaments = [await Tournament.objects.create(name=name) for name in ("a", "b", "c")]
    for event_count, tournament in enumerate(tournaments, start=1):
        for index in range(event_count):
            await Event.objects.create(name=f"{tournament.name}-{index}", tournament=tournament)

    def with_events(minimum: int, desc: str):
        return (
            Tournament.objects.annotate(event_count=Count("events")).filter(event_count__gte=minimum).update(desc=desc)
        )

    await with_events(3, "many")
    updated, hits = await count_plan_hits(with_events(2, "some"))
    assert (updated, hits) == (2, 1)
    assert {tournament.name: tournament.desc for tournament in await Tournament.objects.all()} == {
        "a": None,
        "b": "some",
        "c": "some",
    }


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_update_and_delete_with_a_cte_run_on_their_plan(db):
    await create_numbered_rows()

    def picked(minimum: int):
        return IntFields.objects.filter(id__in=RawSQL('SELECT "id" FROM "picked"')).with_cte(
            "picked", IntFields.objects.filter(intnum__gte=minimum).values("id")
        )

    await picked(6).update(intnum_null=1)
    updated, hits = await count_plan_hits(picked(4).update(intnum_null=2))
    assert (updated, hits) == (3, 1)
    assert sorted(await IntFields.objects.filter(intnum_null=2).values_list("intnum", flat=True)) == [4, 5, 6]
    await picked(6).delete()
    deleted, hits = await count_plan_hits(picked(4).delete())
    assert (deleted, hits) == (2, 1)
    assert sorted(await IntFields.objects.all().values_list("intnum", flat=True)) == [1, 2, 3]


@pytest.mark.asyncio
async def test_update_re_checked_from_its_returning_rows_runs_on_its_plan():
    """A statement whose written values are re-checked from its RETURNING rows keeps which columns
    it re-checks with the plan - the check runs on the plan hit too."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.testmodels"]):
        obj = await IntFields.objects.create(intnum=1)
        await IntFields.objects.filter(id=obj.id).update(intnum=F("intnum") + 1)
        _, hits = await count_plan_hits(IntFields.objects.filter(id=obj.id).update(intnum=F("intnum") + 5))
        assert hits == 1
        assert (await IntFields.objects.get(id=obj.id)).intnum == 7
        with pytest.raises(ValidationError):
            await IntFields.objects.filter(id=obj.id).update(intnum=F("intnum") + 2**40)
        assert (await IntFields.objects.get(id=obj.id)).intnum == 7


@pytest.mark.asyncio
async def test_update_of_a_relation_to_a_composite_key_runs_on_its_plan():
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        targets = [
            await CompositeTarget.objects.create(a=index, b=index * 10, name=f"t{index}") for index in range(1, 4)
        ]
        child = await FkTargetNoConstraint.objects.create(other=targets[0], name="child")
        await FkTargetNoConstraint.objects.filter(id=child.id).update(other=targets[1])
        _, hits = await count_plan_hits(FkTargetNoConstraint.objects.filter(id=child.id).update(other=targets[2]))
        assert hits == 1
        fetched = await FkTargetNoConstraint.objects.get(id=child.id)
        assert (fetched.other_a, fetched.other_b) == (3, 30)


@pytest.mark.asyncio
async def test_update_after_a_keyset_boundary_runs_on_its_plan(db):
    await create_numbered_rows()
    await IntFields.objects.all().order_by("intnum").after_cursor(4).update(intnum_null=1)
    updated, hits = await count_plan_hits(
        IntFields.objects.all().order_by("intnum").after_cursor(2).update(intnum_null=2)
    )
    assert (updated, hits) == (4, 1)
    assert sorted(await IntFields.objects.filter(intnum_null=2).values_list("intnum", flat=True)) == [3, 4, 5, 6]
