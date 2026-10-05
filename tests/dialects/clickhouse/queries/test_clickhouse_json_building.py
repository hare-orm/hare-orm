"""JSONObject() and JSONArray() build JSON text on ClickHouse, each value written as ClickHouse's
``toJSONString()`` writes it - bytes as the hex the column holds, a JSON value or a nested object as
JSON - and filter by a path."""

import datetime
import decimal

import pytest

from hare import fields
from hare.query.expressions import F, Value
from hare.query.functions import Cast, JSONArray, JSONObject, Max
from tests.dialects.clickhouse.models import Player, Revision

JOINED = datetime.datetime(2024, 2, 29, 13, 45, 6, 789000, tzinfo=datetime.UTC)


async def create_players():
    await Player.objects.bulk_create(
        [
            Player(
                id=1,
                name='a "quoted" / name',
                score=decimal.Decimal("100.00"),
                rating=0.1,
                joined=JOINED,
                born=datetime.date(2000, 1, 2),
                active=True,
                data={"tags": ["x"], "level": 3},
            ),
            Player(id=2, name="empty", score=decimal.Decimal("-1.5"), rating=None, active=False),
        ]
    )


@pytest.mark.asyncio
async def test_an_object_holds_each_value_as_clickhouse_writes_it(clickhouse_db):
    await create_players()

    card = JSONObject(
        name="name",
        score="score",
        rating="rating",
        joined="joined",
        born="born",
        active="active",
        data="data",
        id="id",
        twice=F("score") * 2,
        raw=Value(b"\x01\xff"),
        nothing=Value(None),
    )
    assert await Player.objects.order_by("id").annotate(card=card).values_list("card", flat=True) == [
        {
            "name": 'a "quoted" / name',
            "score": 100,
            "rating": 0.1,
            "joined": "2024-02-29 13:45:06.789000",
            "born": "2000-01-02",
            "active": True,
            "data": {"tags": ["x"], "level": 3},
            "id": 1,
            "twice": 200,
            "raw": "01ff",
            "nothing": None,
        },
        {
            "name": "empty",
            "score": -1.5,
            "rating": None,
            "joined": None,
            "born": None,
            "active": False,
            "data": None,
            "id": 2,
            "twice": -3.0,
            "raw": "01ff",
            "nothing": None,
        },
    ]


@pytest.mark.asyncio
async def test_an_array_and_nesting(clickhouse_db):
    await create_players()

    values = JSONArray("id", "active", JSONObject(score="score"), JSONArray(), Value(1e20), Value(float("nan")))
    assert await Player.objects.filter(id=1).annotate(values=values).values_list("values", flat=True) == [
        [1, True, {"score": 100}, [], 10**20, None]
    ]
    assert await Player.objects.filter(id=1).annotate(empty=JSONObject()).values_list("empty", flat=True) == [{}]


@pytest.mark.asyncio
async def test_a_decimal_aggregate_in_json_and_as_text(clickhouse_db):
    await create_players()

    groups = Player.objects.values("active").order_by("active")
    summaries = groups.annotate(summary=JSONObject(top=Max("score"))).values_list("summary", flat=True)
    assert await summaries == [{"top": -1.5}, {"top": 100.0}]
    texts = groups.annotate(text=Cast(Max("score"), fields.TextField())).values_list("text", flat=True)
    assert await texts == ["-1.5", "100"]


@pytest.mark.asyncio
async def test_a_built_object_is_filtered_by_a_path(clickhouse_db):
    await create_players()

    players = Player.objects.annotate(summary=JSONObject(score="score", name="name"))
    assert await players.filter(summary__score__gte=50).values_list("id", flat=True) == [1]
    assert await players.filter(summary__name="empty").values_list("id", flat=True) == [2]


@pytest.mark.asyncio
async def test_an_object_over_a_composite_key_model(clickhouse_db):
    await Revision.objects.bulk_create([Revision(document_id=1, number=2, title="b", score=None)])

    key = JSONObject(document="document_id", number="number", score="score")
    assert await Revision.objects.filter(pk=(1, 2)).annotate(key=key).values_list("key", flat=True) == [
        {"document": 1, "number": 2, "score": None}
    ]
