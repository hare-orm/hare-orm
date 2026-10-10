"""Correlated subqueries on ClickHouse: a scalar subquery reading the query around it runs on a server
of ClickHouse 25.4 on - selected, filtered by, ordered by; an ``EXISTS`` is written as a membership
test whatever the server (ClickHouse's own correlated ``EXISTS`` misses rows); a subquery ordering or
limiting its own rows is refused - the server runs none."""

import decimal

import pytest
import pytest_asyncio

from hare.exceptions import UnSupportedError
from hare.query.expressions import Exists, OuterReference, Subquery
from hare.query.functions import Count, Max, Sum
from tests.dialects.clickhouse.models import Player, Team


@pytest_asyncio.fixture
async def teams(clickhouse_db):
    blue = await Team.objects.create(name="blue")
    red = await Team.objects.create(name="red")
    await Team.objects.create(name="none")
    await Player.objects.bulk_create(
        [
            Player(id=1, name="Ann", team=blue, score=decimal.Decimal("12.5")),
            Player(id=2, name="Bob", team=blue, score=decimal.Decimal("3")),
            Player(id=3, name="Cid", team=red, score=decimal.Decimal("7")),
            Player(id=4, name="Dee"),
        ]
    )
    return {"blue": blue, "red": red}


def players_of_team():
    return Player.objects.filter(team=OuterReference("pk"))


@pytest.mark.asyncio
async def test_a_scalar_correlated_subquery_runs(teams):
    connection = Team._meta.connection
    if not connection.features.supports_correlated_subqueries:
        with pytest.raises(UnSupportedError, match="no correlated subqueries"):
            await Team.objects.annotate(headcount=Subquery(players_of_team().annotate(n=Count("id")).values("n")))
        return
    counted = Team.objects.annotate(
        headcount=Subquery(players_of_team().values("team_id").annotate(n=Count("id")).values("n")),
        best=Subquery(players_of_team().values("team_id").annotate(best=Max("score")).values("best")),
        total=Subquery(players_of_team().values("team_id").annotate(total=Sum("score")).values("total")),
    )
    rows = await counted.order_by("name").values_list("name", "headcount", "best", "total")
    assert rows == [
        ("blue", 2, decimal.Decimal("12.5"), decimal.Decimal("15.5")),
        # A team of no player reads NULL - no row of the subquery.
        ("none", None, None, None),
        ("red", 1, decimal.Decimal("7"), decimal.Decimal("7")),
    ]
    assert await counted.filter(headcount__gte=1).order_by("name").values_list("name", flat=True) == ["blue", "red"]
    assert await counted.filter(total__gt=10).values_list("name", flat=True) == ["blue"]
    by_best = await counted.filter(best__isnull=False).order_by("-best").values_list("name", flat=True)
    assert by_best == ["blue", "red"]
    # Selected and filtered by, sorted by and sliced - the database runs it as a derived table.
    rows = (
        await counted.filter(headcount__gte=1, name__in=["blue", "red"])
        .order_by("-total")
        .values_list("name", "headcount", "total")
    )
    assert rows == [("blue", 2, decimal.Decimal("15.5")), ("red", 1, decimal.Decimal("7"))]
    assert await counted.order_by("-total", "name").values_list("name", flat=True)[:1] == ["blue"]
    teams_by_best = [team.name async for team in counted.filter(best__gt=5).order_by("best")]
    assert teams_by_best == ["red", "blue"]
    assert (await counted.filter(name="red").order_by("best").values("headcount"))[0] == {"headcount": 1}


@pytest.mark.asyncio
async def test_an_exists_is_a_membership_test(teams):
    with_players = Team.objects.annotate(has_players=Exists(players_of_team()))
    assert "EXISTS" not in with_players.sql()
    rows = await with_players.order_by("name").values_list("name", "has_players")
    assert rows == [("blue", True), ("none", False), ("red", True)]
    assert await Team.objects.exclude(players__name="Cid").order_by("name").values_list("name", flat=True) == [
        "blue",
        "none",
    ]


@pytest.mark.asyncio
async def test_a_subquery_ordering_its_own_rows_is_refused(teams):
    first_player = Subquery(players_of_team().order_by("name").values("name")[:1])
    connection = Team._meta.connection
    message = "ordering or limiting" if connection.features.supports_correlated_subqueries else "no correlated"
    with pytest.raises(UnSupportedError, match=message):
        await Team.objects.annotate(first_player=first_player)
