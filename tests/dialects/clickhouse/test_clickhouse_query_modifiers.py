"""ClickHouse's own QuerySet methods - final(), sample() with sample_rows()/sample_offset(), prewhere(),
limit_by(), settings() and with_totals() - and its __global_in/__global_not_in lookups: the SQL they
write, the rows they read and their plans."""

import pytest
import pytest_asyncio

from hare.dialects.clickhouse.query.totals_result import TotalsResult
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import F, Q
from hare.query.functions import Count, Sum
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.dialects.clickhouse.models import PageView, Player, Reading, Team


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
async def test_final_refuses_an_engine_keeping_one_version_and_a_write(readings):
    with pytest.raises(QueryError, match="its engine is MergeTree"):
        await PageView.objects.all().final()
    with pytest.raises(QueryError, match=r"final\(\) changes how rows are read"):
        await Reading.objects.filter(id=1).final().update(value=2)
    with pytest.raises(QueryError, match="takes a bool"):
        await Reading.objects.all().final(all_tables="yes")


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


@pytest.mark.asyncio
async def test_settings_of_reads_and_writes(readings):
    queryset = Reading.objects.final().settings(max_threads=1, use_query_cache=False).settings(max_threads=2)
    assert queryset.sql().endswith("SETTINGS max_threads = 2, use_query_cache = 0")
    assert await queryset.count() == 300
    assert await Reading.objects.filter(id=2).settings(mutations_sync=2).update(value=5) == 1
    assert await Reading.objects.filter(id=2).values_list("value", flat=True) == [5]
    assert await Reading.objects.filter(id=3).settings(max_threads=1).delete() == 1
    assert not await Reading.objects.filter(id=3).exists()
    with pytest.raises(QueryError, match="identifiers"):
        await Reading.objects.settings(**{"max threads": 1})
    for value in (2**64, float("inf"), None, [1]):
        with pytest.raises(QueryError, match="settings"):
            await Reading.objects.settings(max_threads=value)
    with pytest.raises(QueryError, match="at least one"):
        await Reading.objects.settings()


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
    assert first.totals == {"sensor": None, "readings": 300, "total": sum(range(2, 301)) + 1000}
    totals_by_sensor = {
        "s0": sum(range(3, 301, 3)),
        "s1": sum(range(4, 301, 3)) + 1000,
        "s2": sum(range(2, 301, 3)),
    }
    kept = await queryset.filter(total__gt=totals_by_sensor["s2"])
    assert [row["sensor"] for row in kept] == ["s0", "s1"]
    assert kept.totals == {"sensor": None, "readings": 200, "total": totals_by_sensor["s0"] + totals_by_sensor["s1"]}
    empty = await queryset.filter(value__gt=10000)
    assert empty == []
    # SUM of no rows is NULL, as aggregate() reads it.
    assert empty.totals == {"sensor": None, "readings": 0, "total": None}
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
