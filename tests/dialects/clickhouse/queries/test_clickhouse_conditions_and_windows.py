"""CASE, COALESCE and NULLIF of Decimals of different scales, and the window functions ClickHouse names
otherwise or lacks - LAG/LEAD over the whole partition with NULL past its edge, CUME_DIST, and COUNT of
a Nullable column in a window."""

import decimal

import pytest

from hare.exceptions import OperationalError
from hare.query.expressions import Case, F, Value, When, Window
from hare.query.functions import Coalesce, NullIf, Sum
from hare.query.functions.window import Count, CumeDist, DenseRank, Lag, Lead, NTile, Rank, RowNumber
from tests.dialects.clickhouse.models import Member, Player, Revision, Team


async def get_values(expression, **filters):
    return (
        await Player.objects.filter(**filters)
        .order_by("id")
        .annotate(value=expression)
        .values_list("value", flat=True)
    )


async def create_players():
    await Player.objects.bulk_create(
        [
            Player(id=1, name="first", score=decimal.Decimal("1.25"), rating=2.5, active=True),
            Player(id=2, name="second", score=decimal.Decimal("0"), rating=None, active=False),
            Player(id=3, name="third", score=decimal.Decimal("7"), rating=0.5, active=True),
        ]
    )


@pytest.mark.asyncio
async def test_decimals_of_different_scales_in_one_result(clickhouse_db):
    await create_players()

    coalesced = await get_values(Coalesce(NullIf("score", 0), Value(decimal.Decimal("0.125"))))
    assert [str(value) for value in coalesced] == ["1.250", "0.125", "7.000"]
    case = Case(When(active=True, then=F("score")), default=Value(decimal.Decimal("0.5")))
    assert [str(value) for value in await get_values(case)] == ["1.25", "0.50", "7.00"]
    case = Case(When(id=1, then=Value(decimal.Decimal("1.5"))), default=Value(decimal.Decimal("0.25")))
    assert [str(value) for value in await get_values(case)] == ["1.50", "0.25", "0.25"]


@pytest.mark.asyncio
async def test_null_if_compares_decimals_as_numbers(clickhouse_db):
    await create_players()

    assert await get_values(NullIf("score", 0)) == [decimal.Decimal("1.25"), None, decimal.Decimal("7")]
    assert await get_values(NullIf("score", Value(decimal.Decimal("7.000")))) == [
        decimal.Decimal("1.25"),
        decimal.Decimal("0"),
        None,
    ]


@pytest.mark.asyncio
async def test_lag_and_lead_read_the_whole_partition(clickhouse_db):
    await create_players()

    assert await get_values(Window(Lag("id"), order_by=["id"])) == [None, 1, 2]
    assert await get_values(Window(Lead("id"), order_by=["id"])) == [2, 3, None]
    assert await get_values(Window(Lead("id", 2, -1), order_by=["id"])) == [3, -1, -1]
    assert await get_values(Window(Lag("rating"), partition_by=["active"], order_by=["id"])) == [None, None, 2.5]
    assert await get_values(Window(Lead("score"), order_by=["id"])) == [
        decimal.Decimal("0"),
        decimal.Decimal("7"),
        None,
    ]


@pytest.mark.asyncio
async def test_cume_dist(clickhouse_db):
    await create_players()

    assert await get_values(Window(CumeDist(), order_by=["active"])) == [1.0, pytest.approx(1 / 3), 1.0]
    assert await get_values(Window(CumeDist(), order_by=["id"])) == [
        pytest.approx(1 / 3),
        pytest.approx(2 / 3),
        1.0,
    ]


@pytest.mark.asyncio
async def test_a_count_of_a_nullable_column_in_a_window(clickhouse_db):
    """ClickHouse up to 26.9 counts a Nullable column in a window through a subcolumn it then can't
    find - refused with NOT_FOUND_COLUMN_IN_BLOCK, as the dialect's differences say."""
    await create_players()

    if (await Player.get_connection().get_server_version())[:2] <= (26, 9):
        with pytest.raises(OperationalError, match="Not found column"):
            await get_values(Window(Count("rating")))
        return
    assert await get_values(Window(Count("rating"), partition_by=["active"])) == [2, 0, 2]
    assert await get_values(Window(Count("rating"))) == [2, 2, 2]


@pytest.mark.asyncio
async def test_windows_over_a_composite_key_model(clickhouse_db):
    await Revision.objects.bulk_create(
        [
            Revision(document_id=1, number=1, title="a", score=1.5),
            Revision(document_id=1, number=2, title="b", score=None),
            Revision(document_id=2, number=1, title="c", score=4.0),
        ]
    )

    revisions = Revision.objects.order_by("document_id", "number").annotate(
        previous=Window(Lag("score"), partition_by=["document_id"], order_by=["number"]),
        counted=Window(Count("score"), partition_by=["document_id"]),
        share=Window(CumeDist(), partition_by=["document_id"], order_by=["number"]),
    )
    assert await revisions.values_list("previous", "counted", "share") == [
        (None, 1, 0.5),
        (1.5, 1, 1.0),
        (None, 1, 1.0),
    ]


@pytest.mark.asyncio
async def test_a_window_ordered_by_an_aggregate_of_a_relation_groups_the_rows(clickhouse_db):
    """ROW_NUMBER() OVER (ORDER BY SUM(...)) of a relation left the query ungrouped - code 215."""
    first = await Team.objects.create(name="first")
    second = await Team.objects.create(name="second")
    await Member.objects.bulk_create(
        [
            Member(id=1, club=first, number=1),
            Member(id=2, club=first, number=2),
            Member(id=3, club=second, number=9),
        ]
    )
    rows = await Team.objects.annotate(place=Window(RowNumber(), order_by=[Sum("members__number")])).values_list(
        "name", "place"
    )
    assert sorted(rows) == [("first", 1), ("second", 2)]


@pytest.mark.asyncio
async def test_a_row_number_a_rank_and_a_tile_mix_with_a_signed_integer(clickhouse_db):
    """ROW_NUMBER, RANK, DENSE_RANK and NTILE give a UInt64, which ClickHouse finds no common type of
    with a signed integer in CASE or COALESCE."""
    await create_players()

    for function in (RowNumber(), Rank(), DenseRank(), NTile(3)):
        window = Window(function, order_by=["id"])
        assert await get_values(Case(When(id__lte=2, then=window), default=Value(0))) == [1, 2, 0]
        assert await get_values(Coalesce(window, Value(-1))) == [1, 2, 3]


@pytest.mark.asyncio
async def test_rows_are_ordered_by_a_collation(clickhouse_db):
    """Collate() was written COLLATE "en" in parentheses - ClickHouse takes a collation's name as a string
    and only after an ORDER BY key's direction."""
    from hare.query.functions import Collate

    await Player.objects.bulk_create(
        [Player(id=1, name="b", score=decimal.Decimal(0)), Player(id=2, name="A", score=decimal.Decimal(0))]
    )
    names = await Player.objects.order_by(Collate("name", "en").asc()).values_list("name", flat=True)
    assert names == ["A", "b"]
    names = await Player.objects.order_by(Collate("name", "en").desc()).values_list("name", flat=True)
    assert names == ["b", "A"]


@pytest.mark.asyncio
async def test_distinct_rows_are_ordered_by_a_collation(clickhouse_db):
    """distinct() ordered by Collate() selected the collation's ordering in a subquery - ClickHouse takes no
    COLLATE there, and the statement was a syntax error."""
    from hare.query.functions import Collate

    await Player.objects.bulk_create(
        [
            Player(id=1, name="b", score=decimal.Decimal(0)),
            Player(id=2, name="A", score=decimal.Decimal(0)),
            Player(id=3, name="b", score=decimal.Decimal(1)),
        ]
    )
    query = Player.objects.order_by(Collate("name", "en").asc()).distinct().values_list("name", flat=True)
    assert await query == ["A", "b"]


@pytest.mark.asyncio
async def test_distinct_models_are_ordered_by_a_collation(clickhouse_db):
    """distinct() of model rows selected an ordering by Collate() as an expression - ClickHouse takes no
    COLLATE in a selected column."""
    from hare.query.functions import Collate

    await Player.objects.bulk_create(
        [Player(id=1, name="b", score=decimal.Decimal(0)), Player(id=2, name="A", score=decimal.Decimal(0))]
    )
    players = await Player.objects.order_by(Collate("name", "en").asc(), "id").distinct()
    assert [player.name for player in players] == ["A", "b"]
