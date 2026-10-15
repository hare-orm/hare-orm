"""Plain equality reads - ``Model.objects.get(**kwargs)``, ``QuerySet.get()``, ``Model.objects.filter(**kwargs)`` with
or without a ``.limit()`` - run on the statement plan of their shape like any other query: the
first query of a shape builds and records it, every later one binds its values. Each test runs
its queries twice, so both the build and the plan hit return the same rows."""

import datetime
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from hare.exceptions import DoesNotExist
from hare.query.expressions import F, Q, RawSQL
from hare.query.plans.statement.statement_plans import StatementPlans
from tests import testmodels
from tests.testmodels import Author, Book, DecimalFields, Event, IntFields, Tournament


async def run_counting_plan_hits(build: Callable[[], Awaitable[Any]]) -> tuple[Any, int]:
    """Awaits the query ``build`` makes.

    Args:
        build: Makes the query.

    Returns:
        Its result, and how many queries ran on a found plan meanwhile.
    """
    hits = StatementPlans.hits
    result = await build()
    return result, StatementPlans.hits - hits


async def create_authors() -> tuple[list[Author], Author]:
    same = [await Author.objects.create(name="same") for _ in range(5)]
    other = await Author.objects.create(name="other")
    return same, other


@pytest.mark.asyncio
async def test_get_by_primary_key_runs_on_its_plan(db):
    obj = await IntFields.objects.create(intnum=1)
    other = await IntFields.objects.create(intnum=2)
    await IntFields.objects.get(pk=obj.id)
    fetched, hits = await run_counting_plan_hits(lambda: IntFields.objects.get(pk=other.id))
    assert hits == 1
    assert fetched.id == other.id
    assert fetched.intnum == 2


@pytest.mark.asyncio
async def test_get_by_primary_key_field_name(db):
    obj = await IntFields.objects.create(intnum=2)
    for _ in range(2):
        assert (await IntFields.objects.get(id=obj.id)).id == obj.id
    assert (await run_counting_plan_hits(lambda: IntFields.objects.get(id=obj.id)))[1] == 1


@pytest.mark.asyncio
async def test_get_by_uuid_and_char_primary_keys(db):
    uuid_obj = await testmodels.UUIDPkModel.objects.create()
    await testmodels.CharPkModel.objects.create(id="abc-key")
    for _ in range(2):
        assert (await testmodels.UUIDPkModel.objects.get(pk=uuid_obj.id)).id == uuid_obj.id
        assert (await testmodels.CharPkModel.objects.get(pk="abc-key")).id == "abc-key"


@pytest.mark.asyncio
async def test_get_by_primary_key_with_its_own_column_name(db):
    """SourceFields.eyedee has source_field='sometable_id' - the statement compares the real
    column, under ``pk`` and under the field's name."""
    obj = await testmodels.SourceFields.objects.create(chars="x")
    for _ in range(2):
        assert (await testmodels.SourceFields.objects.get(pk=obj.eyedee)).eyedee == obj.eyedee
        assert (await testmodels.SourceFields.objects.get(eyedee=obj.eyedee)).eyedee == obj.eyedee


@pytest.mark.asyncio
async def test_get_without_a_match_raises(db):
    for _ in range(2):
        with pytest.raises(DoesNotExist):
            await IntFields.objects.get(pk=10**9)


@pytest.mark.asyncio
async def test_get_by_other_fields(db):
    author = await Author.objects.create(name="author-of-the-book")
    book = await Book.objects.create(name="unique-book-name", author=author, rating=4.5)
    await IntFields.objects.create(intnum=42)
    for _ in range(2):
        assert (await IntFields.objects.get(intnum=42)).intnum == 42
        assert (await Book.objects.get(name="unique-book-name", id=book.id)).id == book.id
        assert (await Book.objects.get(id=book.id, name="unique-book-name")).id == book.id
    assert (await run_counting_plan_hits(lambda: Book.objects.get(id=book.id, name="unique-book-name")))[1] == 1


@pytest.mark.asyncio
async def test_get_with_conditions(db):
    author = await Author.objects.create(name="author-of-the-book")
    book = await Book.objects.create(name="positional-q-book", author=author, rating=3.0)
    queries = [
        lambda: Book.objects.get(Q(pk=book.id)),
        lambda: Book.objects.get(Q(name="positional-q-book")),
        lambda: Book.objects.get(Q(id=book.id), Q(name="positional-q-book")),
        lambda: Book.objects.get(Q(id=book.id), name="positional-q-book"),
        lambda: Book.objects.get(Q(id=book.id, name="positional-q-book")),
        lambda: Book.objects.get(Q(Q(id=book.id))),
        lambda: Book.objects.get(Q(Q(id=book.id), Q(name="positional-q-book"))),
        lambda: Book.objects.get(Q(id=book.id), ~Q(name="another book")),
        lambda: Book.objects.filter(name="positional-q-book").get(),
        lambda: Book.objects.filter(id=book.id).first(),
        lambda: Book.objects.get(id=book.id).offset(0),
    ]
    for build in queries:
        assert (await build()).id == book.id
        fetched, hits = await run_counting_plan_hits(build)
        assert fetched.id == book.id
        assert hits == 1


@pytest.mark.asyncio
async def test_get_with_other_lookups(db):
    first = await IntFields.objects.create(intnum=1, intnum_null=None)
    second = await IntFields.objects.create(intnum=2, intnum_null=5)
    for _ in range(2):
        assert (await IntFields.objects.filter(intnum__gt=1).get()).id == second.id
        assert (await IntFields.objects.filter(pk__in=[first.id]).get()).id == first.id
        assert (await IntFields.objects.filter(intnum__in=[2, 3]).get()).id == second.id
        assert (await IntFields.objects.get(intnum_null__isnull=True)).id == first.id
        assert (await IntFields.objects.get(intnum_null__isnull=False)).id == second.id
        assert (await IntFields.objects.get(pk=first.id).only("intnum")).intnum == 1


@pytest.mark.asyncio
async def test_get_by_none_compares_with_is_null(db):
    """``field=None`` is an ``IS NULL`` - a plan of its own, never the plan of ``field = value``."""
    first = await IntFields.objects.create(intnum=1, intnum_null=None)
    second = await IntFields.objects.create(intnum=2, intnum_null=5)
    await IntFields.objects.get(intnum_null=5)
    for round_index in range(2):
        fetched, hits = await run_counting_plan_hits(lambda: IntFields.objects.get(intnum_null=None))
        assert fetched.id == first.id
        assert hits == round_index
        assert (await IntFields.objects.get(intnum_null=5)).id == second.id
        with pytest.raises(DoesNotExist):
            await IntFields.objects.get(pk=None)


@pytest.mark.asyncio
async def test_get_by_an_expression_value(db):
    """An ``F()`` value is resolved by the filter engine (here with its own JOIN) and binds
    nothing - a later query of the same plan key runs on the plan the first one kept."""
    tournament = await Tournament.objects.create(name="T1")
    matching = await Event.objects.create(name="T1", tournament=tournament)
    await Event.objects.create(name="another", tournament=tournament)
    await Event.objects.get(name="another")
    # The first query builds the plan unless an earlier test of this process already did.
    assert (await Event.objects.get(name=F("tournament__name"))).pk == matching.pk
    fetched, hits = await run_counting_plan_hits(lambda: Event.objects.get(name=F("tournament__name")))
    assert fetched.pk == matching.pk
    assert hits == 1


@pytest.mark.asyncio
async def test_get_by_a_raw_sql_value(db):
    tournament = await Tournament.objects.create(name="only")
    await Tournament.objects.get(pk=tournament.pk)
    for _ in range(2):
        assert await Tournament.objects.get(pk=RawSQL("id")) == tournament


@pytest.mark.asyncio
async def test_get_by_a_subquery_value(db):
    _, other = await create_authors()
    for _ in range(2):
        assert await Author.objects.get(id=Author.objects.filter(name="other").values_list("id", flat=True)) == other


@pytest.mark.asyncio
async def test_get_through_a_relation(db):
    author = await Author.objects.create(name="someone")
    book = await Book.objects.create(name="a book", author=author, rating=1)
    for _ in range(2):
        assert (await Book.objects.filter(author__name="someone").get()).id == book.id


@pytest.mark.asyncio
async def test_get_with_prefetch(db):
    parent = await testmodels.UUIDPkModel.objects.create()
    for _ in range(2):
        fetched = await testmodels.UUIDPkModel.objects.get(pk=parent.id).prefetch_related("children")
        assert list(fetched.children) == []


@pytest.mark.asyncio
async def test_get_by_composite_primary_key(db):
    thing = await testmodels.CompositePkThing.objects.create(thing_id=1, revision=1, name="composite")
    for _ in range(2):
        assert (await testmodels.CompositePkThing.objects.get(pk=(1, 1))).name == thing.name
        assert (await testmodels.CompositePkThing.objects.get(thing_id=1, revision=1)).name == thing.name


@pytest.mark.asyncio
async def test_decimal_equality_is_not_rounded(db):
    """A filter value is bound with the lookup conversion, not the write one, which rounds a
    DecimalField value to its decimal places - get(decimal_nodec=Decimal("1.5")) must not find
    the row holding 2."""
    for value in range(4):
        await DecimalFields.objects.create(decimal=Decimal(value), decimal_nodec=Decimal(value))
    for _ in range(2):
        assert await DecimalFields.objects.filter(decimal_nodec=Decimal("1.5")) == []
        assert await DecimalFields.objects.filter(decimal_nodec=Decimal("1.5")).limit(5) == []
        with pytest.raises(DoesNotExist):
            await DecimalFields.objects.get(decimal_nodec=Decimal("1.5"))
        assert (await DecimalFields.objects.get(decimal_nodec=Decimal("2"))).decimal == Decimal(2)


@pytest.mark.asyncio
async def test_fetched_instance_flags(db):
    obj = await IntFields.objects.create(intnum=1)
    tracked = await testmodels.DirtyTrackedThing.objects.create(name="a", count=1)
    for _ in range(2):
        fetched = await IntFields.objects.get(pk=obj.id)
        assert fetched._saved_in_db is True
        assert fetched._partial is False
        assert fetched._custom_generated_pk is False  # "id" is a generated (auto-increment) pk
        assert fetched._await_when_save == {}
        # hydrate_rows() doesn't know about dirty tracking - execute_select() takes the snapshot.
        assert (await testmodels.DirtyTrackedThing.objects.get(pk=tracked.id)).get_dirty_fields() == {}
        assert (await testmodels.DirtyTrackedThing.objects.filter(id=tracked.id))[0].get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_every_field_type_is_read(db):
    date_obj = await testmodels.DateFields.objects.create(date=datetime.date(2024, 3, 15), date_null=None)
    delta = datetime.timedelta(days=1, hours=2, minutes=3)
    delta_obj = await testmodels.TimeDeltaFields.objects.create(timedelta=delta, timedelta_null=None)
    time_obj = await testmodels.TimeFields.objects.create(time=datetime.time(12, 30, 45), time_null=None)
    uuid_obj = await testmodels.UUIDFields.objects.create(data=uuid.uuid4(), data_null=None)
    decimal_obj = await testmodels.DecimalFields.objects.create(
        decimal=Decimal("1234.5678"), decimal_nodec=Decimal("42"), decimal_null=None
    )
    json_obj = await testmodels.JSONFields.objects.create(data={"a": 1, "b": [1, 2, 3]}, data_null=None)
    enum_obj = await testmodels.EnumFields.objects.create(
        service=testmodels.Service.python_programming, currency=testmodels.Currency.EUR
    )
    for _ in range(2):
        date_fetched = await testmodels.DateFields.objects.get(pk=date_obj.id)
        assert date_fetched.date == datetime.date(2024, 3, 15)
        assert date_fetched.date_null is None
        delta_fetched = await testmodels.TimeDeltaFields.objects.get(pk=delta_obj.id)
        assert delta_fetched.timedelta == delta
        assert delta_fetched.timedelta_null is None
        time_fetched = await testmodels.TimeFields.objects.get(pk=time_obj.id)
        assert time_fetched.time.replace(tzinfo=None) == datetime.time(12, 30, 45)
        assert time_fetched.time_null is None
        assert (await testmodels.UUIDFields.objects.get(pk=uuid_obj.id)).data == uuid_obj.data
        decimal_fetched = await testmodels.DecimalFields.objects.get(pk=decimal_obj.id)
        assert decimal_fetched.decimal == Decimal("1234.5678")
        assert decimal_fetched.decimal_null is None
        assert (await testmodels.JSONFields.objects.get(pk=json_obj.id)).data == {"a": 1, "b": [1, 2, 3]}
        enum_fetched = await testmodels.EnumFields.objects.get(pk=enum_obj.id)
        assert enum_fetched.service == testmodels.Service.python_programming
        assert enum_fetched.currency == testmodels.Currency.EUR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        lambda value: IntFields.objects.get(intnum=value),
        lambda value: IntFields.objects.get(Q(intnum=value)),
        lambda value: IntFields.objects.filter(intnum=value),
    ],
    ids=["Model.get", "get with a condition", "filter"],
)
async def test_row_reader_is_reused_across_queries_of_a_shape(db, build):
    """Queries of one shape read their rows with one compiled hydrate function, not one compiled
    per query."""
    from hare.query.plans.statement.statement_plans import StatementPlans

    await IntFields.objects.create(intnum=100)
    await IntFields.objects.create(intnum=101)
    await build(100)
    reader_count = len(StatementPlans.hydrate_functions)
    await build(101)
    assert len(StatementPlans.hydrate_functions) == reader_count


@pytest.mark.asyncio
async def test_filter_rows_run_on_their_plan(db):
    same, _ = await create_authors()
    await Author.objects.filter(name="other")
    rows, hits = await run_counting_plan_hits(lambda: Author.objects.filter(name="same"))
    assert hits == 1
    assert sorted(row.id for row in rows) == sorted(author.id for author in same)
    assert all(row._saved_in_db and not row._partial for row in rows)


@pytest.mark.asyncio
async def test_sliced_and_unsliced_filters_keep_separate_plans(db):
    """The LIMIT and OFFSET values are bound per query, but their presence is part of the SQL
    text: a sliced query never waits behind the plan an unsliced one recorded, or the reverse."""
    same, _ = await create_authors()
    queries = [
        (lambda: Author.objects.filter(name="same"), 5),
        (lambda: Author.objects.filter(name="same").limit(3), 3),
        (lambda: Author.objects.filter(name="same").limit(2), 2),
        (lambda: Author.objects.filter(name="same").order_by("id").offset(4), 1),
        (lambda: Author.objects.filter(name="same").order_by("id").limit(2).offset(1), 2),
    ]
    for build, _expected_count in queries:
        await build()
    for build, expected_count in queries:
        rows, hits = await run_counting_plan_hits(build)
        assert hits == 1
        assert len(rows) == expected_count
        assert {row.name for row in rows} == {"same"}
        assert {row.id for row in rows} <= {author.id for author in same}


@pytest.mark.asyncio
async def test_limited_filter_reads_the_rows_an_ordered_one_does(db):
    await create_authors()
    for _ in range(2):
        for limit in (None, 1, 2, 5, 10):
            plain = Author.objects.filter(name="same")
            ordered = Author.objects.filter(name="same").order_by("id")
            plain_rows = await (plain if limit is None else plain.limit(limit))
            ordered_rows = await (ordered if limit is None else ordered.limit(limit))
            assert len(plain_rows) == len(ordered_rows)


@pytest.mark.asyncio
async def test_filter_by_several_fields_and_without_a_match(db):
    await create_authors()
    target = await Author.objects.filter(name="same").first()
    for _ in range(2):
        assert [row.id for row in await Author.objects.filter(name="same", id=target.id)] == [target.id]
        assert await Author.objects.filter(name="nobody").limit(2) == []


@pytest.mark.asyncio
async def test_queryset_awaited_twice_reads_the_same_rows(db):
    await create_authors()
    queryset = Author.objects.filter(name="same").order_by("id").limit(2)
    first = await queryset
    second, hits = await run_counting_plan_hits(lambda: queryset)
    assert hits == 1
    assert [row.id for row in first] == [row.id for row in second]


@pytest.mark.asyncio
async def test_model_with_default_ordering_keeps_its_order(db):
    tournament = await Tournament.objects.create(name="t")
    for name in ("c", "a", "b"):
        await Event.objects.create(name=name, tournament=tournament)
    for _ in range(2):
        rows = await Event.objects.filter(tournament_id=tournament.id)
        assert [row.name for row in rows] == ["a", "b", "c"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "expected_names"),
    [
        (lambda: Author.objects.filter(name="same").order_by("-id").limit(2), ["same", "same"]),
        (lambda: Author.objects.filter(name="same").offset(4), ["same"]),
        (lambda: Author.objects.filter(name="same").limit(2).offset(1), ["same", "same"]),
        (lambda: Author.objects.exclude(name="same"), ["other"]),
        (lambda: Author.objects.filter(Q(name="other") | Q(name="nobody")), ["other"]),
        (lambda: Author.objects.filter(name__startswith="oth"), ["other"]),
        (lambda: Author.objects.filter(name=F("name"), id__gte=0).filter(name="other"), ["other"]),
        (lambda: Author.objects.filter(name=RawSQL("'other'")), ["other"]),
        (
            lambda: Author.objects.filter(id=Author.objects.filter(name="other").values_list("id", flat=True)),
            ["other"],
        ),
        (lambda: Author.objects.filter(name="other").distinct(), ["other"]),
        (lambda: Author.objects.filter(name="other").only("id", "name"), ["other"]),
    ],
    ids=[
        "order_by",
        "offset",
        "limit+offset",
        "exclude",
        "or",
        "non-equality lookup",
        "F value",
        "RawSQL value",
        "subquery value",
        "distinct",
        "only",
    ],
)
async def test_other_shapes_read_their_rows(db, build, expected_names):
    await create_authors()
    for _ in range(2):
        assert [row.name for row in await build()] == expected_names


@pytest.mark.asyncio
async def test_filter_through_a_relation(db):
    author = await Author.objects.create(name="writer")
    await Book.objects.create(name="one", author=author, rating=1)
    for _ in range(2):
        assert [row.name for row in await Book.objects.filter(author__name="writer")] == ["one"]
