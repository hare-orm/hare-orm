import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.query.expressions import F, RawSQL, Subquery
from hare.query.functions import Avg, Count, Sum, Upper
from tests.testmodels import Author, Book, Event, Team, Tournament


@pytest_asyncio.fixture
async def group_by_data(db):
    """Set up Author and Book data for group_by tests."""
    a1 = await Author.objects.create(name="author1")
    a2 = await Author.objects.create(name="author2")
    books1 = [await Book.objects.create(name=f"book{i}", author=a1, rating=i) for i in range(10)]
    books2 = [await Book.objects.create(name=f"book{i}", author=a2, rating=i) for i in range(5)]
    return {"a1": a1, "a2": a2, "books1": books1, "books2": books2}


@pytest.mark.asyncio
async def test_count_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    ret = await Book.objects.annotate(count=Count("id")).group_by("author_id").values("author_id", "count")

    for item in ret:
        author_id = item.get("author_id")
        count = item.get("count")
        if author_id == a1.pk:
            assert count == 10
        elif author_id == a2.pk:
            assert count == 5


@pytest.mark.asyncio
async def test_count_and_exists_match_the_instances_of_a_group_by_queryset(db, group_by_data):
    """Model instances are always grouped by their primary key, so .group_by() doesn't change
    which rows `await qs` returns - count()/exists() agree with those rows."""
    queryset = Book.objects.annotate(count=Count("id")).group_by("author_id")
    assert len(await queryset) == 15
    assert await queryset.count() == 15

    exists = await queryset.exists()
    assert exists is True

    having_queryset = queryset.filter(count__gte=2)
    assert await having_queryset == []
    assert await having_queryset.count() == 0
    assert await having_queryset.exists() is False
    assert await having_queryset.update(name="never") == 0
    assert await having_queryset.delete() == 0

    no_match_exists = (
        await Book.objects.filter(name="does-not-exist").annotate(count=Count("id")).group_by("author_id").exists()
    )
    assert no_match_exists is False


@pytest.mark.asyncio
async def test_count_group_by_with_join(db, group_by_data):
    ret = await Book.objects.annotate(count=Count("id")).group_by("author__name").values("author__name", "count")
    assert sorted(ret, key=lambda x: x["author__name"]) == sorted(
        [{"author__name": "author1", "count": 10}, {"author__name": "author2", "count": 5}],
        key=lambda x: x["author__name"],
    )


@pytest.mark.asyncio
async def test_count_filter_group_by(db, group_by_data):
    ret = (
        await Book.objects.annotate(count=Count("id"))
        .filter(count__gt=6)
        .group_by("author_id")
        .values("author_id", "count")
    )
    assert len(ret) == 1
    assert ret[0].get("count") == 10


@pytest.mark.asyncio
async def test_sum_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    ret = await Book.objects.annotate(sum=Sum("rating")).group_by("author_id").values("author_id", "sum")
    for item in ret:
        author_id = item.get("author_id")
        sum_ = item.get("sum")
        if author_id == a1.pk:
            assert sum_ == 45.0
        elif author_id == a2.pk:
            assert sum_ == 10.0


@pytest.mark.asyncio
async def test_sum_group_by_with_join(db, group_by_data):
    ret = await Book.objects.annotate(sum=Sum("rating")).group_by("author__name").values("author__name", "sum")
    assert sorted(ret, key=lambda x: x["author__name"]) == sorted(
        [{"author__name": "author1", "sum": 45.0}, {"author__name": "author2", "sum": 10.0}],
        key=lambda x: x["author__name"],
    )


@pytest.mark.asyncio
async def test_sum_filter_group_by(db, group_by_data):
    ret = (
        await Book.objects.annotate(sum=Sum("rating"))
        .filter(sum__gt=11)
        .group_by("author_id")
        .values("author_id", "sum")
    )
    assert len(ret) == 1
    assert ret[0].get("sum") == 45.0


@pytest.mark.asyncio
async def test_avg_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    ret = await Book.objects.annotate(avg=Avg("rating")).group_by("author_id").values("author_id", "avg")

    for item in ret:
        author_id = item.get("author_id")
        avg = item.get("avg")
        if author_id == a1.pk:
            assert avg == 4.5
        elif author_id == a2.pk:
            assert avg == 2.0


@pytest.mark.asyncio
async def test_avg_group_by_with_join(db, group_by_data):
    ret = await Book.objects.annotate(avg=Avg("rating")).group_by("author__name").values("author__name", "avg")
    assert sorted(ret, key=lambda x: x["author__name"]) == sorted(
        [{"author__name": "author1", "avg": 4.5}, {"author__name": "author2", "avg": 2}],
        key=lambda x: x["author__name"],
    )


@pytest.mark.asyncio
async def test_avg_filter_group_by(db, group_by_data):
    ret = (
        await Book.objects.annotate(avg=Avg("rating"))
        .filter(avg__gt=3)
        .group_by("author_id")
        .values_list("author_id", "avg")
    )
    assert len(ret) == 1
    assert ret[0][1] == 4.5


@pytest.mark.asyncio
async def test_count_values_list_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    ret = await Book.objects.annotate(count=Count("id")).group_by("author_id").values_list("author_id", "count")

    for item in ret:
        author_id = item[0]
        count = item[1]
        if author_id == a1.pk:
            assert count == 10
        elif author_id == a2.pk:
            assert count == 5


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_count_values_list_group_by_does_not_duplicate_annotation_sql(db, group_by_data):
    """values_list() re-registers an annotation under its positional alias without dropping the
    original annotate()-given key, which previously caused _get_annotate() to select the
    same aggregate expression twice (once under each key) - wasted work, not wrong results
    (the correct value still landed at the right tuple position)."""
    qs = Book.objects.annotate(count=Count("id")).group_by("author_id").values_list("author_id", "count")
    sql = qs.sql(parameters_inline=True)
    # A column that holds no NULL: a dialect may count the rows instead of reading it.
    assert sql.count('COUNT("id")') + sql.count("COUNT(*)") == 1


@pytest.mark.asyncio
async def test_count_values_list_group_by_with_join(db, group_by_data):
    ret = await Book.objects.annotate(count=Count("id")).group_by("author__name").values_list("author__name", "count")
    assert sorted(ret) == sorted([("author1", 10), ("author2", 5)])


@pytest.mark.asyncio
async def test_count_values_list_filter_group_by(db, group_by_data):
    ret = (
        await Book.objects.annotate(count=Count("id"))
        .filter(count__gt=6)
        .group_by("author_id")
        .values_list("author_id", "count")
    )
    assert len(ret) == 1
    assert ret[0][1] == 10


@pytest.mark.asyncio
async def test_sum_values_list_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    ret = await Book.objects.annotate(sum=Sum("rating")).group_by("author_id").values_list("author_id", "sum")
    for item in ret:
        author_id = item[0]
        sum_ = item[1]
        if author_id == a1.pk:
            assert sum_ == 45.0
        elif author_id == a2.pk:
            assert sum_ == 10.0


@pytest.mark.asyncio
async def test_sum_values_list_group_by_with_join(db, group_by_data):
    ret = await Book.objects.annotate(sum=Sum("rating")).group_by("author__name").values_list("author__name", "sum")
    assert sorted(ret) == sorted([("author1", 45.0), ("author2", 10.0)])


# ============================================================================
# Auto GROUP BY on a purely local (no-join) aggregate - the outer gate on
# _apply_auto_group_by() used to also require a join/.having()/.order_by() before applying any
# GROUP BY at all, so a plain aggregate annotate() with none of those got no GROUP BY whatsoever:
# SQLite silently picked one arbitrary row per group (wrong result, no error), Postgres raised
# "must appear in the GROUP BY clause" outright.
# ============================================================================


@pytest.mark.asyncio
async def test_auto_group_by_local_aggregate_without_join_having_or_order_by(db):
    team_a = await Team.objects.create(name="Team A")
    team_b = await Team.objects.create(name="Team B")
    team_c = await Team.objects.create(name="Team C")

    teams = await Team.objects.all().annotate(total=Sum("id"))

    assert len(teams) == 3
    totals_by_id = {team.id: team.total for team in teams}
    assert totals_by_id == {team_a.id: team_a.id, team_b.id: team_b.id, team_c.id: team_c.id}


@pytest.mark.asyncio
async def test_sum_values_list_filter_group_by(db, group_by_data):
    ret = (
        await Book.objects.annotate(sum=Sum("rating"))
        .filter(sum__gt=11)
        .group_by("author_id")
        .values_list("author_id", "sum")
    )
    assert len(ret) == 1
    assert ret[0][1] == 45.0


@pytest.mark.asyncio
async def test_avg_values_list_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    ret = await Book.objects.annotate(avg=Avg("rating")).group_by("author_id").values_list("author_id", "avg")

    for item in ret:
        author_id = item[0]
        avg = item[1]
        if author_id == a1.pk:
            assert avg == 4.5
        elif author_id == a2.pk:
            assert avg == 2.0


@pytest.mark.asyncio
async def test_avg_values_list_group_by_with_join(db, group_by_data):
    ret = await Book.objects.annotate(avg=Avg("rating")).group_by("author__name").values_list("author__name", "avg")
    assert sorted(ret) == sorted([("author1", 4.5), ("author2", 2.0)])


@pytest.mark.asyncio
async def test_implicit_group_by(db, group_by_data):
    ret = await Author.objects.annotate(count=Count("books")).filter(count__gt=6)
    assert ret[0].count == 10


@pytest.mark.asyncio
async def test_group_by_annotate_result(db, group_by_data):
    ret = (
        await Book.objects.annotate(upper_name=Upper("author__name"), count=Count("id"))
        .group_by("upper_name")
        .values("upper_name", "count")
    )
    assert sorted(ret, key=lambda x: x["upper_name"]) == sorted(
        [{"upper_name": "AUTHOR1", "count": 10}, {"upper_name": "AUTHOR2", "count": 5}],
        key=lambda x: x["upper_name"],
    )


@pytest.mark.asyncio
async def test_group_by_requiring_nested_joins(db):
    tournament_first = await Tournament.objects.create(name="Tournament 1", desc="d1")
    tournament_second = await Tournament.objects.create(name="Tournament 2", desc="d2")

    event_first = await Event.objects.create(name="1", tournament=tournament_first)
    event_second = await Event.objects.create(name="2", tournament=tournament_first)
    event_third = await Event.objects.create(name="3", tournament=tournament_second)

    team_first = await Team.objects.create(name="First", alias=2)
    team_second = await Team.objects.create(name="Second", alias=4)
    team_third = await Team.objects.create(name="Third", alias=5)

    await team_first.events.add(event_first)
    await team_second.events.add(event_second)
    await team_third.events.add(event_third)

    ret = (
        await Tournament.objects.annotate(avg=Avg("events__participants__alias"))
        .group_by("desc")
        .order_by("desc")
        .values("desc", "avg")
    )
    assert ret == [{"avg": 3, "desc": "d1"}, {"avg": 5, "desc": "d2"}]


@pytest.mark.asyncio
async def test_group_by_ambigious_column(db):
    tournament_first = await Tournament.objects.create(name="Tournament 1")
    tournament_second = await Tournament.objects.create(name="Tournament 2")

    await Event.objects.create(name="1", tournament=tournament_first)
    await Event.objects.create(name="2", tournament=tournament_first)
    await Event.objects.create(name="3", tournament=tournament_second)

    base_query = Tournament.objects.annotate(event_count=Count("events")).group_by("name").order_by("name")
    ret = await base_query.values("name", "event_count")
    assert ret == [
        {"event_count": 2, "name": "Tournament 1"},
        {"event_count": 1, "name": "Tournament 2"},
    ]

    ret = await base_query.values_list("name", "event_count")
    assert ret == [("Tournament 1", 2), ("Tournament 2", 1)]


@pytest.mark.asyncio
async def test_group_by_nested_column(db):
    tournament_first = await Tournament.objects.create(name="A")
    tournament_second = await Tournament.objects.create(name="B")

    await Event.objects.create(name="1", tournament=tournament_first)
    await Event.objects.create(name="2", tournament=tournament_first)
    await Event.objects.create(name="3", tournament=tournament_first)
    await Event.objects.create(name="4", tournament=tournament_second)

    base_query = (
        Event.objects.annotate(count=Count("event_id")).group_by("tournament__name").order_by("-tournament__name")
    )
    ret = await base_query.values("tournament__name", "count")
    assert ret == [
        {"count": 1, "tournament__name": "B"},
        {"count": 3, "tournament__name": "A"},
    ]

    ret = await base_query.values_list("tournament__name", "count")
    assert ret == [("B", 1), ("A", 3)]


@pytest.mark.asyncio
async def test_group_by_id_with_nested_filter(db, group_by_data):
    books1 = group_by_data["books1"]

    ret = await Book.objects.filter(author__name="author1").group_by("id").values_list("id")
    assert set(ret) == {(book.id,) for book in books1}


@pytest.mark.asyncio
async def test_select_subquery_with_group_by(db, group_by_data):
    a1 = group_by_data["a1"]
    a2 = group_by_data["a2"]

    subquery = Subquery(Book.objects.all().group_by("rating").order_by("-rating").limit(1).values("rating"))
    ret = await Author.objects.annotate(top_rating=subquery).order_by("id").values_list("name", "top_rating")
    assert ret == [(a1.name, 9.0), (a2.name, 9.0)]


@pytest.mark.asyncio
async def test_group_by_and_order_by_unselected_annotation_with_parameter(db, group_by_data):
    """group_by() and order_by() of an annotation carrying a bind parameter, left out of
    values_list(): its expression is rendered in both clauses, and Postgres only accepts the
    ORDER BY one when it carries the same parameter as the GROUP BY one."""
    arithmetic = (
        await Book.objects.annotate(rating_bucket=F("rating") / 5)
        .annotate(count=Count("id"))
        .group_by("rating_bucket")
        .order_by("rating_bucket")
        .values_list("count", flat=True)
    )
    raw = (
        await Book.objects.annotate(is_high=RawSQL("rating > %s", [6]))
        .annotate(count=Count("id"))
        .group_by("is_high")
        .order_by("is_high")
        .values_list("count", flat=True)
    )

    assert sum(arithmetic) == 15 and len(arithmetic) == 10
    assert raw == [12, 3]


@pytest.mark.asyncio
async def test_group_by_unselected_annotation_is_not_served_from_a_stale_cache(db, group_by_data):
    """The GROUP BY/ORDER BY expression of an unselected annotation isn't refreshed from the
    query shape cache - a second call with another literal must not reuse the first one's."""
    counts_by_multiplier = {}
    for multiplier in (1, 0):
        counts_by_multiplier[multiplier] = (
            await Book.objects.annotate(rating_bucket=F("rating") * multiplier)
            .annotate(count=Count("id"))
            .group_by("rating_bucket")
            .order_by("rating_bucket")
            .values_list("count", flat=True)
        )

    assert len(counts_by_multiplier[1]) == 10
    assert counts_by_multiplier[0] == [15]
