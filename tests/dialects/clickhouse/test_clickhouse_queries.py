"""Reads and writes of hare's queries on ClickHouse: values of every field type round-trip, filters,
ordering, aggregation, relations and the mutations - their counts included."""

import datetime
import decimal
import uuid

import pytest
import pytest_asyncio

from hare.exceptions import IntegrityError, ValidationError
from hare.query.expressions import Exists, F, OuterReference, Q, Subquery, Value, Window
from hare.query.functions import (
    Avg,
    Concat,
    Count,
    Extract,
    Length,
    Lower,
    Max,
    Min,
    StdDev,
    Sum,
    Trunc,
    Upper,
    Variance,
)
from hare.query.functions.window import RowNumber
from tests.dialects.clickhouse.models import Player, Skill, Team

JOINED = datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def league(clickhouse_db):
    blue = await Team.objects.create(name="blue")
    red = await Team.objects.create(name="red", description="it's \\ red")
    sprint = await Skill.objects.create(id=1, name="sprint")
    await Skill.objects.create(id=2, name="pass")
    ann = await Player.objects.create(
        id=1,
        name="Ann O'Neil",
        team=blue,
        score=decimal.Decimal("12.50"),
        rating=4.5,
        joined=JOINED,
        born=datetime.date(1990, 5, 6),
        data={"position": "goal", "numbers": [1, 7], "captain": True},
    )
    await Player.objects.create(id=2, name="Bob", team=blue, score=decimal.Decimal("3.25"), rating=2.0)
    await Player.objects.create(id=3, name="Cid", team=red, score=decimal.Decimal("7"), active=False)
    await Player.objects.create(id=4, name="Dee", data={"position": "wing"})
    await ann.skills.add(sprint)
    return {"blue": blue, "red": red}


@pytest.mark.asyncio
async def test_every_field_type_round_trips(league):
    row = await Player.objects.filter(id=1).values(
        "name", "score", "rating", "joined", "born", "active", "notes", "data", "team_id"
    )
    assert row == [
        {
            "name": "Ann O'Neil",
            "score": decimal.Decimal("12.50"),
            "rating": 4.5,
            "joined": JOINED,
            "born": datetime.date(1990, 5, 6),
            "active": True,
            "notes": None,
            "data": {"position": "goal", "numbers": [1, 7], "captain": True},
            "team_id": league["blue"].id,
        }
    ]
    team = await Team.objects.get(name="red")
    assert isinstance(team.id, uuid.UUID)
    assert team.description == "it's \\ red"


@pytest.mark.asyncio
async def test_filters(league):
    async def ids(**filters):
        return await Player.objects.filter(**filters).values_list("id", flat=True)

    assert await ids(name__icontains="o'n") == [1]
    assert await ids(name__startswith="B") == [2]
    assert await ids(name__endswith="e") == [4]
    assert await ids(name__iexact="cid") == [3]
    assert await ids(id__in=[1, 3, 99]) == [1, 3]
    assert await ids(score__gte=decimal.Decimal("7")) == [1, 3]
    assert await ids(score__range=(decimal.Decimal("3"), decimal.Decimal("8"))) == [2, 3]
    assert await ids(rating__isnull=True) == [3, 4]
    assert await ids(team__isnull=True) == [4]
    assert await ids(team__name="red") == [3]
    assert await ids(joined__year=2024, joined__month=1, joined__day=2) == [1]
    assert await ids(joined__gt=JOINED - datetime.timedelta(microseconds=1)) == [1]
    assert await ids(born__lt=datetime.date(2000, 1, 1)) == [1]
    assert await ids(active=False) == [3]
    assert await ids(data__position="wing") == [4]
    assert await ids(name__contains="%") == []
    assert await ids(name__contains="_") == []


@pytest.mark.asyncio
async def test_ordering_slicing_and_null_order(league):
    assert await Player.objects.order_by("-score").values_list("id", flat=True) == [1, 3, 2, 4]
    assert await Player.objects.order_by("id").offset(1).limit(2).values_list("id", flat=True) == [2, 3]
    assert await Player.objects.order_by("rating", "id").values_list("id", flat=True) == [2, 1, 3, 4]
    assert (await Player.objects.order_by("id").first()).id == 1


@pytest.mark.asyncio
async def test_a_negated_condition_on_a_subquery(league):
    """`NOT (SELECT ...) IS NULL` was read as the function call NOT(SELECT ...) - a syntax error."""
    top_score = Subquery(Player.objects.filter(team__isnull=False).order_by("-score").values("score")[:1])
    with_top = Player.objects.annotate(top=top_score)
    assert await with_top.filter(top__isnull=False).order_by("id").values_list("id", flat=True) == [1, 2, 3, 4]
    assert await with_top.exclude(top__isnull=False).count() == 0


@pytest.mark.asyncio
async def test_subtracting_a_negative_value(league):
    """`"id"-$1` with -2 was sent as `"id"--2`, the rest of the statement a comment."""
    shifted = Player.objects.annotate(shifted=F("id") - (-2) + 3).filter(id__lte=2).order_by("id")
    assert await shifted.values_list("shifted", flat=True) == [6, 7]
    assert await Player.objects.filter(id__gt=F("id") - (-2) - 3).order_by("id").values_list("id", flat=True) == [
        1,
        2,
        3,
        4,
    ]


@pytest.mark.asyncio
async def test_aggregation(league):
    totals = await Player.objects.aggregate(
        total=Sum("score"), average=Avg("rating"), most=Max("score"), least=Min("score"), count=Count("id")
    )
    assert totals == {
        "total": decimal.Decimal("22.75"),
        "average": 3.25,
        "most": decimal.Decimal("12.50"),
        "least": decimal.Decimal("0.00"),
        "count": 4,
    }
    per_team = (
        await Team.objects.annotate(players_count=Count("players"))
        .order_by("name")
        .values_list("name", "players_count")
    )
    assert per_team == [("blue", 2), ("red", 1)]


@pytest.mark.asyncio
async def test_aggregates_over_no_rows_are_null(league):
    """ClickHouse gives a column type's default (0, 1970-01-01) for an aggregate over no rows - hare
    writes each by its -OrNull form, so the result is NULL as in SQL; a count stays 0."""
    no_rows = Player.objects.filter(id=-1)
    assert await no_rows.aggregate(
        total=Sum("score"),
        distinct_total=Sum("id", distinct=True),
        filtered_total=Sum("score", _filter=Q(active=True)),
        most=Max("score"),
        least=Min("id"),
        average=Avg("id"),
        deviation=StdDev("score"),
        variance=Variance("id"),
        count=Count("id"),
    ) == {
        "total": None,
        "distinct_total": None,
        "filtered_total": None,
        "most": None,
        "least": None,
        "average": None,
        "deviation": None,
        "variance": None,
        "count": 0,
    }
    assert await Player.objects.aggregate(
        average_id=Avg("id"), filtered_total=Sum("score", _filter=Q(active=True))
    ) == {"average_id": 2.5, "filtered_total": decimal.Decimal("15.75")}


@pytest.mark.asyncio
async def test_an_aggregate_of_a_group_keeps_its_plain_form(league):
    """A group holds at least one row, so its aggregates are written plainly - the -OrNull form
    stays where a FILTER can leave an aggregate no row. A group whose values are all NULL still
    gives NULL."""
    per_team = (
        Player.objects.values("team_id")
        .annotate(
            total=Sum("score"),
            rated=Avg("rating"),
            active_total=Sum("score", _filter=Q(active=True)),
            players=Count("id"),
            rated_players=Count("rating"),
        )
        .order_by("-total")
    )
    sql = per_team.sql()
    assert 'SUM("score") "total",AVG("rating") "rated"' in sql
    assert 'sumOrNull("score") FILTER(' in sql
    assert 'COUNT(*) "players",COUNT("rating") "rated_players"' in sql
    blue, red = league["blue"], league["red"]
    assert await per_team == [
        {
            "team_id": blue.id,
            "total": decimal.Decimal("15.75"),
            "rated": 3.25,
            "active_total": decimal.Decimal("15.75"),
            "players": 2,
            "rated_players": 2,
        },
        {
            "team_id": red.id,
            "total": decimal.Decimal("7.00"),
            "rated": None,
            "active_total": None,
            "players": 1,
            "rated_players": 0,
        },
        {
            "team_id": None,
            "total": decimal.Decimal("0.00"),
            "rated": None,
            "active_total": decimal.Decimal("0.00"),
            "players": 1,
            "rated_players": 0,
        },
    ]
    over_five = Player.objects.values("team_id").annotate(total=Sum("score")).filter(total__gt=5)
    assert 'HAVING SUM("score")>' in over_five.sql()
    assert sorted(row["total"] for row in await over_five) == [decimal.Decimal("7.00"), decimal.Decimal("15.75")]


@pytest.mark.asyncio
async def test_a_moment_is_truncated_in_a_zone(league):
    """The stored moment is truncated as it is - in UTC, or in the zone asked for - and read back
    as a moment of the column's own precision."""
    truncated = Player.objects.filter(id=1).annotate(
        day=Trunc("joined", "day"),
        hour=Trunc("joined", "hour"),
        tokyo_day=Trunc("joined", "day", tzinfo="Asia/Tokyo"),
    )
    assert "dateTrunc('day', toTimeZone(\"joined\", 'UTC'))" in truncated.sql()
    day, hour, tokyo_day = (await truncated.values_list("day", "hour", "tokyo_day"))[0]
    assert day == datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    assert hour == datetime.datetime(2024, 1, 2, 3, tzinfo=datetime.UTC)
    # 03:04 UTC is 12:04 in Tokyo - its day began at 15:00 UTC the day before.
    assert tokyo_day == datetime.datetime(2024, 1, 1, 15, tzinfo=datetime.UTC)
    assert await Player.objects.filter(id=2).annotate(day=Trunc("joined", "day")).values_list("day", flat=True) == [
        None
    ]


@pytest.mark.asyncio
async def test_a_value_written_from_an_expression_is_checked_before_the_write(league):
    """ClickHouse has no RETURNING to read a written value back - the value an expression writes is
    computed by a SELECT first, and a value the field refuses leaves the row as it was."""
    assert await Player.objects.filter(id=2).update(score=F("score") + 1) == 1
    player = await Player.objects.get(id=3)
    player.score = F("score") * 2
    await player.save()
    assert await Player.objects.filter(id__in=[2, 3]).order_by("id").values_list("score", flat=True) == [
        decimal.Decimal("4.25"),
        decimal.Decimal("14.00"),
    ]
    await Player.objects.filter(id=2).update(score=decimal.Decimal("99999999.50"))
    with pytest.raises(ValidationError):
        await Player.objects.filter(id=2).update(score=F("score") + 1)
    player = await Player.objects.get(id=2)
    player.score = F("score") + 1
    with pytest.raises(ValidationError):
        await player.save()
    assert await Player.objects.filter(id=2).values_list("score", flat=True) == [decimal.Decimal("99999999.50")]


@pytest.mark.asyncio
async def test_relations(league):
    players = await Player.objects.select_related("team").order_by("id")
    assert [(player.id, player.team.name if player.team else None) for player in players] == [
        (1, "blue"),
        (2, "blue"),
        (3, "red"),
        (4, None),
    ]
    team = await Team.objects.prefetch_related("players").get(name="blue")
    assert sorted(player.id for player in team.players) == [1, 2]
    assert await Player.objects.filter(skills__name="sprint").values_list("id", flat=True) == [1]
    assert await Player.objects.filter(skills__isnull=True).values_list("id", flat=True) == [2, 3, 4]
    # Several JOINs: ClickHouse named a result column "player.id" - the instances lacked their fields.
    joined = await Player.objects.filter(skills__name="sprint").select_related("team").order_by("id")
    assert [(player.id, player.name, player.team.name) for player in joined] == [(1, "Ann O'Neil", "blue")]
    counted = await Player.objects.annotate(skills_count=Count("skills")).select_related("team").order_by("id")
    assert [(player.id, player.skills_count) for player in counted] == [(1, 1), (2, 0), (3, 0), (4, 0)]


@pytest.mark.asyncio
async def test_a_window_function_next_to_an_aggregate(league):
    """The columns a window function partitions by weren't grouped - ClickHouse refused the query."""
    numbered = Player.objects.annotate(
        skills_count=Count("skills"), number=Window(RowNumber(), partition_by=["active"], order_by=["id"])
    ).order_by("-number", "id")
    assert await numbered.values_list("id", "number", "skills_count") == [(4, 3, 0), (2, 2, 0), (1, 1, 1), (3, 1, 0)]


@pytest.mark.asyncio
async def test_correlated_conditions_run_as_membership_tests(league):
    assert await Team.objects.exclude(players__name="Cid").order_by("name").values_list("name", flat=True) == ["blue"]
    with_scores = Team.objects.annotate(
        has_high=Exists(Player.objects.filter(team=OuterReference("pk"), score__gt=decimal.Decimal("10")))
    )
    assert await with_scores.filter(has_high=True).values_list("name", flat=True) == ["blue"]
    sql = with_scores.sql()
    assert "EXISTS" not in sql


@pytest.mark.asyncio
async def test_mutations_count_their_rows(league):
    assert await Player.objects.filter(team=league["blue"]).update(active=False) == 2
    assert await Player.objects.filter(active=False).count() == 3
    player = await Player.objects.get(id=4)
    player.name = "Dora"
    await player.save()
    assert await Player.objects.filter(id=4).values_list("name", flat=True) == ["Dora"]
    assert await Player.objects.filter(id__in=[3, 4]).delete() == 2
    assert await Player.objects.filter(id=99).delete() == 0
    assert await Player.objects.count() == 2


@pytest.mark.asyncio
async def test_a_mutation_filtered_across_a_missing_related_row(league):
    """The subquery of a mutation's condition runs without the session's join_use_nulls: a player
    without a team read the team's name as '' instead of NULL - every player was updated."""
    assert await Player.objects.exclude(team__name__isnull=True).update(notes="in a team") == 3
    assert await Player.objects.filter(notes="in a team").order_by("id").values_list("id", flat=True) == [1, 2, 3]
    assert await Player.objects.filter(team__name__isnull=True).delete() == 1
    assert await Player.objects.order_by("id").values_list("id", flat=True) == [1, 2, 3]


@pytest.mark.asyncio
async def test_bulk_writes(clickhouse_db):
    await Player.objects.bulk_create([Player(id=index, name=f"p{index}") for index in range(10, 20)])
    players = await Player.objects.filter(id__lt=13)
    for player in players:
        player.rating = float(player.id)
    await Player.objects.bulk_update(players, ["rating"])
    assert await Player.objects.filter(rating__isnull=False).values_list("id", "rating") == [
        (10, 10.0),
        (11, 11.0),
        (12, 12.0),
    ]


@pytest.mark.asyncio
async def test_bulk_update_writes_each_row_its_own_values(clickhouse_db):
    await Player.objects.bulk_create([Player(id=index, name=f"p{index}") for index in range(5)])
    players = await Player.objects.filter(id__in=[1, 3])
    for player in players:
        player.rating = player.id / 2
        player.name = f"renamed {player.id}"
    # A row the table doesn't hold changes nothing.
    ghost = Player(id=99, name="ghost", rating=9.0)
    ghost._saved_in_db = True
    await Player.objects.bulk_update([*players, ghost], ["rating", "name"])
    assert await Player.objects.order_by("id").values_list("id", "name", "rating") == [
        (0, "p0", None),
        (1, "renamed 1", 0.5),
        (2, "p2", None),
        (3, "renamed 3", 1.5),
        (4, "p4", None),
    ]


@pytest.mark.asyncio
async def test_a_key_is_unique(clickhouse_db):
    # ClickHouse keeps no uniqueness - hare checks the key before the row is written.
    await Skill.objects.create(id=5, name="first")
    with pytest.raises(IntegrityError, match="primary key"):
        await Skill.objects.create(id=5, name="second")
    assert await Skill.objects.filter(id=5).values_list("name", flat=True) == ["first"]


@pytest.mark.asyncio
async def test_exclude_keeps_the_rows_a_null_leaves_unmatched(league):
    assert await Player.objects.exclude(rating=None).values_list("id", flat=True) == [1, 2]
    assert await Player.objects.exclude(rating=4.5).values_list("id", flat=True) == [2, 3, 4]


@pytest.mark.asyncio
async def test_set_operations_drop_repeated_rows(league):
    blue, red = league["blue"].id, league["red"].id
    team_ids = Player.objects.values_list("team_id", flat=True)
    active_team_ids = Player.objects.filter(active=True).values_list("team_id", flat=True)
    red_team_ids = Player.objects.filter(team_id=red).values_list("team_id", flat=True)

    union = await team_ids.union(active_team_ids)
    assert len(union) == 3 and set(union) == {blue, red, None}
    intersection = await team_ids.intersection(active_team_ids)
    assert len(intersection) == 2 and set(intersection) == {blue, None}
    difference = await team_ids.difference(red_team_ids)
    assert len(difference) == 2 and set(difference) == {blue, None}


@pytest.mark.asyncio
async def test_a_decimal_literal_keeps_its_scale_in_arithmetic(league):
    ann = await Player.objects.get(id=1)
    ann.score = F("score") + decimal.Decimal("1.5")
    await ann.save()
    await Player.objects.filter(id=2).update(score=F("score") + decimal.Decimal("0.75"))

    assert await Player.objects.filter(id__in=[1, 2]).values_list("score", flat=True) == [
        decimal.Decimal("14.00"),
        decimal.Decimal("4.00"),
    ]
    assert await Player.objects.filter(id=2).annotate(value=F("score") * decimal.Decimal("1.5")).values_list(
        "value", flat=True
    ) == [decimal.Decimal("6.000")]
    assert await Player.objects.filter(id=1).annotate(value=F("rating") + 0.1).values_list("value", flat=True) == [4.6]
    assert await Player.objects.filter(id__lt=decimal.Decimal("1.5")).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_text_lookups_on_a_nullable_column(league):
    await Player.objects.filter(id=2).update(notes="Fast on the wing")

    assert await Player.objects.filter(notes__contains="wing").values_list("id", flat=True) == [2]
    assert await Player.objects.filter(notes__icontains="FAST").values_list("id", flat=True) == [2]
    assert await Player.objects.filter(notes__startswith="Fast").values_list("id", flat=True) == [2]
    assert await Player.objects.filter(id__contains="3").values_list("id", flat=True) == [3]


@pytest.mark.asyncio
async def test_text_functions_annotated(league):
    rows = (
        await Player.objects.filter(id__in=[1, 2])
        .annotate(lower=Lower("name"), upper=Upper("name"), length=Length("name"))
        .values_list("lower", "upper", "length")
    )

    assert rows == [("ann o'neil", "ANN O'NEIL", 10), ("bob", "BOB", 3)]


@pytest.mark.asyncio
async def test_the_microseconds_of_a_moment_before_1970(league):
    await Player.objects.filter(id=2).update(
        joined=datetime.datetime(1960, 5, 1, 10, 0, 0, 250000, tzinfo=datetime.UTC)
    )

    assert await Player.objects.filter(id=2).annotate(value=Extract("joined", "microsecond")).values_list(
        "value", flat=True
    ) == [250000]


@pytest.mark.asyncio
async def test_concat_writes_every_type_as_text(league):
    rows = (
        await Player.objects.filter(id=1)
        .annotate(
            number=Concat("name", Value("/"), F("id")),
            day=Concat("name", Value("/"), F("born")),
            team_key=Concat("name", Value("/"), F("team_id")),
            moment=Concat("name", Value("/"), F("joined")),
            flag=Concat("name", Value("/"), F("active")),
            missing=Concat("name", Value("/"), F("notes")),
        )
        .values("number", "day", "team_key", "moment", "flag", "missing")
    )

    assert rows == [
        {
            "number": "Ann O'Neil/1",
            "day": "Ann O'Neil/1990-05-06",
            "team_key": f"Ann O'Neil/{league['blue'].id}",
            "moment": "Ann O'Neil/2024-01-02 03:04:05.123456+00:00",
            "flag": "Ann O'Neil/true",
            "missing": "Ann O'Neil/",
        }
    ]


@pytest.mark.asyncio
async def test_json_key_lookups(league):
    # A JSON column holds an object at its top.
    await Player.objects.filter(id=2).update(data={"goal": 3})
    await Player.objects.filter(id=3).update(data={"numbers": "x"})

    assert await Player.objects.filter(data__has_key="position").values_list("id", flat=True) == [1, 4]
    assert await Player.objects.filter(data__has_key="goal").values_list("id", flat=True) == [2]
    assert await Player.objects.filter(data__has_keys=["position", "numbers"]).values_list("id", flat=True) == [1]
    assert await Player.objects.filter(data__has_any_keys=["numbers", "goal"]).values_list("id", flat=True) == [
        1,
        2,
        3,
    ]
    assert await Player.objects.exclude(data__has_key="position").values_list("id", flat=True) == [2, 3]
    assert await Player.objects.filter(data__contains={"position": "goal"}).values_list("id", flat=True) == [1]
    assert await Player.objects.filter(data__contains={"numbers": [7]}).values_list("id", flat=True) == [1]
    assert await Player.objects.filter(data__contains={"numbers": [7, 2]}).values_list("id", flat=True) == []
    assert await Player.objects.filter(data__contained_by={"position": "wing", "x": 1}).values_list(
        "id", flat=True
    ) == [4]
    assert await Player.objects.filter(data__contained_by={"goal": 3.0, "numbers": "x"}).values_list(
        "id", flat=True
    ) == [2, 3]
