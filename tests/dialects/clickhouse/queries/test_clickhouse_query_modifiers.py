"""ClickHouse's own QuerySet methods - final(), sample() with sample_rows()/sample_offset(), prewhere(),
limit_by(), settings() and with_totals() - and its __global_in/__global_not_in lookups: the SQL they
write, the rows they read and their plans."""

import pytest
import pytest_asyncio

from hare.contrib.test import capture_queries
from hare.dialects.clickhouse.query.totals_result import TotalsResult
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import F, Q
from hare.query.functions import Count, Sum
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.dialects.clickhouse.models import KeyedQuote, PageView, Player, Reading, Team


@pytest_asyncio.fixture
async def readings(clickhouse_db):
    await Reading.objects.bulk_create(
        [Reading(id=number, sensor=f"s{number % 3}", value=number, version=0) for number in range(1, 301)]
    )
    # A second version of row 1, merged into the last one by FINAL.
    await Reading.objects.bulk_create([Reading(id=1, sensor="s1", value=1000, version=1)])


@pytest.mark.asyncio
async def test_final_reads_the_merged_version_of_a_row(readings):
    assert sorted(await Reading.objects.filter(id=1).values_list("value", flat=True)) == [1, 1000]
    queryset = Reading.objects.filter(id=1).final()
    assert " FINAL" in queryset.sql()
    assert await queryset.values_list("value", flat=True) == [1000]
    assert await queryset.count() == 1
    assert [reading.value for reading in await queryset] == [1000]
    all_tables = Reading.objects.filter(id=1).final(all_tables=True)
    assert all_tables.sql().endswith("SETTINGS final = 1")
    assert await all_tables.values_list("value", flat=True) == [1000]


@pytest.mark.asyncio
async def test_a_row_read_through_final_is_saved(readings):
    """save() compared the row read with the first stored version, an older one - a false
    UnSupportedError."""
    reading = await Reading.objects.final().get(id=1)
    assert reading.value == 1000
    reading.value = 7
    await reading.save()
    assert await Reading.objects.final().filter(id=1).values_list("value", flat=True) == [7]
    reading.value = 8
    await reading.save(update_fields=["value"])
    assert await Reading.objects.final().filter(id=1).values_list("value", flat=True) == [8]


@pytest.mark.asyncio
async def test_final_refuses_an_engine_keeping_one_version_and_a_write(readings):
    with pytest.raises(QueryError, match="its engine is MergeTree"):
        await PageView.objects.all().final()
    with pytest.raises(QueryError, match=r"final\(\) changes how rows are read"):
        await Reading.objects.filter(id=1).final().update(value=2)


@pytest.mark.asyncio
async def test_sample_reads_a_share_and_its_offset_the_rest(readings):
    queryset = Reading.objects.final()
    first_half = set(await queryset.sample(50).values_list("id", flat=True))
    second_half = set(await queryset.sample(50).sample_offset(50).values_list("id", flat=True))
    assert " SAMPLE 0.5 OFFSET 0.5" in queryset.sample(50).sample_offset(50).sql()
    assert first_half and second_half
    assert not first_half & second_half
    assert first_half | second_half == set(range(1, 301))
    assert await queryset.sample(100).count() == 300
    assert " SAMPLE 0.125" in Reading.objects.sample(12.5).sql()
    rows = await Reading.objects.final().sample_rows(100).values_list("id", flat=True)
    # At least the rows asked for, read by whole granules - all of them where one granule holds them.
    assert 0 < len(rows) <= 300
    assert " SAMPLE 100" in Reading.objects.sample_rows(100).sql()


@pytest.mark.asyncio
async def test_sample_refusals(readings):
    with pytest.raises(QueryError, match="needs ClickhouseTableOptions"):
        await Player.objects.sample(10)
    with pytest.raises(QueryError, match="needs ClickhouseTableOptions"):
        await Player.objects.sample_rows(10)
    with pytest.raises(UnSupportedError, match="no seed"):
        await Reading.objects.sample(10, seed=1)
    with pytest.raises(UnSupportedError, match="no method but"):
        await Reading.objects.sample(10, method="system")
    with pytest.raises(UnSupportedError, match="use none"):
        await Reading.objects.sample(0)
    with pytest.raises(QueryError, match="there is none"):
        await Reading.objects.sample_offset(10)
    with pytest.raises(QueryError, match="sample_rows"):
        await Reading.objects.sample(10).sample_rows(100)
    for rows in (1, True, 1.5, -2):
        with pytest.raises(QueryError, match="sample_rows"):
            await Reading.objects.sample_rows(rows)
    for percent in (100, -1, float("nan"), "1"):
        with pytest.raises(QueryError, match="sample_offset"):
            await Reading.objects.sample(10).sample_offset(percent)


@pytest.mark.asyncio
async def test_prewhere_reads_like_a_filter_and_keeps_a_plan(readings):
    queryset = Reading.objects.final().prewhere(value__gt=290).order_by("id")
    assert " PREWHERE " in queryset.sql()
    # Row 1's last version has the value 1000.
    assert await queryset.values_list("id", flat=True) == [1, *range(291, 301)]
    assert await queryset.count() == 11
    assert await Reading.objects.final().prewhere(Q(sensor="s0") | Q(sensor="s2"), value__lte=6).order_by(
        "id"
    ).values_list("id", flat=True) == [2, 3, 5, 6]
    await Reading.objects.final().prewhere(value__gt=295).order_by("id").values_list("id", flat=True)
    hits = StatementPlans.hits
    assert await Reading.objects.final().prewhere(value__gt=297).order_by("id").values_list("id", flat=True) == [
        1,
        298,
        299,
        300,
    ]
    assert StatementPlans.hits == hits + 1
    with pytest.raises(QueryError, match="own table"):
        await Player.objects.prewhere(team__name="blue")
    with pytest.raises(QueryError, match=r"prewhere\(\) changes how rows are read"):
        await Reading.objects.prewhere(id=1).delete()


@pytest.mark.asyncio
async def test_limit_by_keeps_rows_per_group(readings):
    queryset = Reading.objects.final().order_by("sensor", "-value").limit_by(2, "sensor")
    assert " LIMIT 2 BY " in queryset.sql()
    rows = await queryset.values_list("sensor", "value")
    assert rows == [("s0", 300), ("s0", 297), ("s1", 1000), ("s1", 298), ("s2", 299), ("s2", 296)]
    assert await queryset.count() == 6
    assert await queryset.exists()
    assert len(await queryset) == 6
    skipped = Reading.objects.final().order_by("sensor", "-value").limit_by(1, "sensor", offset=1)
    assert await skipped.values_list("value", flat=True) == [297, 298, 296]
    assert await skipped.count() == 3
    # Row 1's last version has the even value 1000.
    by_expression = Reading.objects.final().order_by("id").limit_by(1, F("value") % 2)
    assert await by_expression.values_list("id", flat=True) == [1, 3]
    with pytest.raises(QueryError, match="aggregate"):
        await queryset.aggregate(total=Sum("value"))
    with pytest.raises(QueryError, match=r"limit_by\(\) changes how rows are read"):
        await Reading.objects.limit_by(2, "sensor").update(value=1)
    for arguments in ((-1, "sensor"), (True, "sensor"), (1,), (1, 2)):
        with pytest.raises(QueryError, match="limit_by"):
            await Reading.objects.limit_by(*arguments)


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.asyncio
async def test_an_iterator_reads_the_rows_limit_by_keeps(readings, offset):
    """The iterator paged by WHERE id > the last row's - the condition applied before LIMIT BY, which picked
    the rows of each group anew on every page."""
    queryset = Reading.objects.filter(id__lte=12).order_by("id").limit_by(2, "sensor", offset=offset)
    expected = await queryset.values_list("id", flat=True)
    assert len(expected) == 6
    assert [row.id async for row in queryset.iterator(chunk_size=1)] == expected
    assert [row["id"] async for row in queryset.values("id").iterator(chunk_size=2)] == expected
    assert [row_id async for row_id in queryset.values_list("id", flat=True).iterator(chunk_size=4)] == expected


@pytest.mark.asyncio
async def test_how_rows_are_read_applies_to_a_distinct_ordered_by_a_field_it_doesnt_select(readings):
    """A distinct() ordered by a field it doesn't select runs over that field too, as Django runs it."""
    for queryset in (
        Reading.objects.final(),
        Reading.objects.final().sample(100),
        Reading.objects.final().sample(50).sample_offset(0),
        Reading.objects.final().prewhere(value__gt=0),
    ):
        sensors = await queryset.order_by("id").values_list("sensor", flat=True).distinct()
        assert len(sensors) > 3 and set(sensors) == {"s0", "s1", "s2"}


@pytest.mark.asyncio
async def test_limit_by_the_primary_key_of_a_composite_key(clickhouse_db):
    """limit_by("pk") of a composite key raised FieldError - it groups by each of its columns."""
    await KeyedQuote.objects.bulk_create(
        [KeyedQuote(symbol=symbol, quoted_at=moment, price=moment) for symbol in "AB" for moment in (1, 2)]
    )
    queryset = KeyedQuote.objects.order_by("symbol", "quoted_at").limit_by(1, "pk")
    assert "LIMIT 1 BY " in queryset.sql()
    assert await queryset.values_list("symbol", "quoted_at") == [("A", 1), ("A", 2), ("B", 1), ("B", 2)]
    assert await KeyedQuote.objects.order_by("quoted_at").limit_by(1, "symbol").count() == 2


@pytest.mark.asyncio
async def test_settings_of_reads_and_writes(readings):
    queryset = Reading.objects.final().settings(max_threads=1, use_query_cache=False).settings(max_threads=2)
    assert queryset.sql().endswith("SETTINGS max_threads = 2, use_query_cache = 0")
    assert await queryset.count() == 300
    await Reading.objects.filter(id=2).settings(mutations_sync=2).update(value=5)
    assert await Reading.objects.filter(id=2).values_list("value", flat=True) == [5]
    await Reading.objects.filter(id=3).settings(max_threads=1).delete()
    assert not await Reading.objects.filter(id=3).exists()
    with pytest.raises(QueryError, match="identifiers"):
        await Reading.objects.settings(**{"max threads": 1})
    for value in (2**64, float("inf"), None, [1]):
        with pytest.raises(QueryError, match="settings"):
            await Reading.objects.settings(max_threads=value)
    with pytest.raises(QueryError, match="at least one"):
        await Reading.objects.settings()


@pytest.mark.asyncio
async def test_with_totals_reads_the_rows_and_their_totals_in_one_statement(readings):
    """The totals row was read by a second statement - neither driver returned the one WITH TOTALS adds."""
    queryset = Reading.objects.final().values("sensor").annotate(total=Sum("value")).order_by("sensor").with_totals()
    assert " GROUP BY " in queryset.sql() and " WITH TOTALS" in queryset.sql()
    async with capture_queries() as counter:
        rows = await queryset.filter(value__lte=3)
    assert len(counter.queries) == 1
    assert rows == [{"sensor": "s0", "total": 3}, {"sensor": "s2", "total": 2}]
    assert rows.totals == {"sensor": "", "total": 5}


@pytest.mark.asyncio
async def test_with_totals_adds_the_aggregates_over_every_group(readings):
    queryset = (
        Reading.objects.final()
        .values("sensor")
        .annotate(readings=Count("id"), total=Sum("value"))
        .order_by("sensor")
        .with_totals()
    )
    first = await queryset[:1]
    assert isinstance(first, TotalsResult)
    assert first == [{"sensor": "s0", "readings": 100, "total": sum(range(3, 301, 3))}]
    # The grouped field holds its type's default value in the totals row, as ClickHouse writes it.
    assert first.totals == {"sensor": "", "readings": 300, "total": sum(range(2, 301)) + 1000}
    totals_by_sensor = {
        "s0": sum(range(3, 301, 3)),
        "s1": sum(range(4, 301, 3)) + 1000,
        "s2": sum(range(2, 301, 3)),
    }
    kept = await queryset.filter(total__gt=totals_by_sensor["s2"])
    assert [row["sensor"] for row in kept] == ["s0", "s1"]
    assert kept.totals == {"sensor": "", "readings": 200, "total": totals_by_sensor["s0"] + totals_by_sensor["s1"]}
    empty = await queryset.filter(value__gt=10000)
    assert empty == []
    # SUM of no rows is ClickHouse's own 0, as aggregate() reads it.
    assert empty.totals == {"sensor": "", "readings": 0, "total": 0}
    with pytest.raises(QueryError, match="grouped values"):
        await Reading.objects.with_totals()
    with pytest.raises(QueryError, match="grouped values"):
        await Team.objects.values("name").with_totals()


@pytest.mark.asyncio
async def test_global_in_reads_as_in(readings):
    queryset = Reading.objects.final().filter(id__global_in=[1, 2, None]).order_by("id")
    assert " GLOBAL IN " in queryset.sql()
    assert await queryset.values_list("id", flat=True) == [1, 2]
    assert await Reading.objects.final().filter(id__global_not_in=[*range(3, 301)]).order_by("id").values_list(
        "id", flat=True
    ) == [1, 2]
    subquery = Reading.objects.final().filter(value__gt=298).values("id")
    assert await Reading.objects.final().filter(id__global_in=subquery).order_by("id").values_list(
        "id", flat=True
    ) == [1, 299, 300]
    assert await Reading.objects.final().filter(id__global_not_in=subquery).count() == 297
    assert await Reading.objects.filter(id__global_in=[]).count() == 0
    blue = await Team.objects.create(name="blue")
    await Player.objects.create(id=1, name="Ann", team=blue)
    await Player.objects.create(id=2, name="Bob")
    assert await Player.objects.filter(team__global_in=[blue.id]).values_list("name", flat=True) == ["Ann"]
    assert await Player.objects.filter(team__global_not_in=[blue.id]).values_list("name", flat=True) == ["Bob"]


@pytest.mark.asyncio
async def test_a_delete_reaching_related_rows_refuses_how_rows_are_read_too(clickhouse_db):
    # Team has players - its delete picks the rows first, then deletes them with what relates to them.
    await Team.objects.create(name="blue")
    for queryset, method_name in (
        (Team.objects.prewhere(name="blue"), "prewhere"),
        (Team.objects.order_by("name").limit_by(1, "name"), "limit_by"),
    ):
        with pytest.raises(QueryError, match=rf"{method_name}\(\) changes how rows are read"):
            await queryset.delete()
    assert await Team.objects.count() == 1


@pytest.mark.asyncio
async def test_with_totals_of_no_rows(readings):
    """ClickHouse's totals row over no rows: a count of 0, and each other aggregate and grouped field its
    column type's default value."""
    rows = await (
        Reading.objects.filter(value__lt=-1)
        .values("sensor")
        .annotate(readings=Count("id"), total=Sum("value"))
        .with_totals()
    )
    assert list(rows) == []
    assert rows.totals == {"sensor": "", "readings": 0, "total": 0}
