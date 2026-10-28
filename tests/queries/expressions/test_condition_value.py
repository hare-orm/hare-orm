"""A condition as a value of each row (ConditionValue) - a Q given to annotate(), alias(), values(),
values_list() or update(): true where filter() takes the row, false where it doesn't, NULL where SQL's
three-valued logic gives NULL."""

import pytest
import pytest_asyncio

from hare.query.expressions import ConditionValue, Exists, OuterReference, Q
from hare.query.functions import Count
from tests.shared_models.testmodels import BooleanFields, Event, Tournament


@pytest_asyncio.fixture
async def tournaments(db):
    """Tournament 1 with no description and an event, 2 described "x", 3 described "y"."""
    first = await Tournament.objects.create(id=1, name="a", desc=None)
    await Tournament.objects.create(id=2, name="b", desc="x")
    await Tournament.objects.create(id=3, name="c", desc="y")
    await Event.objects.create(event_id=1, name="e", tournament=first)


def get_has_event() -> Exists:
    return Exists(Event.objects.filter(tournament_id=OuterReference("id")))


CONDITIONS = {
    "isnull": lambda: Q(desc__isnull=True),
    "equal": lambda: Q(desc="x"),
    "negated": lambda: ~Q(name="a"),
    "or": lambda: Q(desc="x") | Q(name="a"),
    "exists": lambda: Q(get_has_event()),
    "exists_or_exists": lambda: get_has_event() | Exists(Event.objects.filter(name="none")),
    "exists_or_q": lambda: get_has_event() | Q(name="c"),
    "and": lambda: Q(desc__isnull=False) & ~Q(name="b"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("condition_name", CONDITIONS)
async def test_the_value_is_true_exactly_on_the_rows_filter_takes(tournaments, condition_name):
    condition = CONDITIONS[condition_name]()

    values = dict(await Tournament.objects.annotate(holds=condition).order_by("id").values_list("id", "holds"))

    taken_ids = set(await Tournament.objects.filter(condition).values_list("id", flat=True))
    assert {tournament_id for tournament_id, holds in values.items() if holds} == taken_ids
    assert all(holds in (True, False, None) for holds in values.values())


@pytest.mark.asyncio
async def test_a_condition_on_null_is_null(tournaments):
    values = await Tournament.objects.annotate(is_x=Q(desc="x")).order_by("id").values_list("is_x", flat=True)

    assert values == [None, True, False]


@pytest.mark.asyncio
async def test_an_annotated_condition_is_filtered_and_ordered_by(tournaments):
    queryset = Tournament.objects.annotate(described=Q(desc__isnull=False))

    assert await queryset.filter(described=True).order_by("id").values_list("id", flat=True) == [2, 3]
    assert await queryset.filter(described=False).values_list("id", flat=True) == [1]
    assert await queryset.order_by("described", "-id").values_list("id", flat=True) == [1, 3, 2]
    objs = await queryset.order_by("id")
    assert [obj.described for obj in objs] == [False, True, True]


@pytest.mark.asyncio
async def test_a_comparison_condition_is_read_as_a_bool_and_compared_with_one(tournaments):
    queryset = Tournament.objects.annotate(after_a=Q(name__gt="a"), is_x=Q(desc="x")).order_by("id")

    rows = await queryset.values_list("after_a", "is_x")
    assert rows == [(False, None), (True, True), (True, False)]
    assert {type(value) for row in rows for value in row if value is not None} == {bool}
    assert {type(obj.after_a) for obj in await queryset} == {bool}
    assert await queryset.filter(after_a=True).values_list("id", flat=True) == [2, 3]
    assert await queryset.aggregate(before_b=Count("id", _filter=Q(after_a=False))) == {"before_b": 1}


@pytest.mark.asyncio
async def test_an_aliased_condition_is_filtered_by(tournaments):
    queryset = Tournament.objects.alias(has_event=Q(get_has_event()))

    assert await queryset.filter(has_event=True).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_values_and_values_list_take_a_condition(tournaments):
    assert await Tournament.objects.order_by("id").values("id", has_event=Q(get_has_event())) == [
        {"id": 1, "has_event": True},
        {"id": 2, "has_event": False},
        {"id": 3, "has_event": False},
    ]
    assert await Tournament.objects.order_by("id").values_list("id", is_b=Q(name="b")) == [
        (1, False),
        (2, True),
        (3, False),
    ]


@pytest.mark.asyncio
async def test_update_writes_a_condition(db):
    for row_id, boolean in ((1, True), (2, False)):
        await BooleanFields.objects.create(id=row_id, boolean=boolean)

    await BooleanFields.objects.all().update(boolean_null=Q(boolean=False) | Q(id=1))

    assert await BooleanFields.objects.order_by("id").values_list("boolean_null", flat=True) == [True, True]
    await BooleanFields.objects.all().update(boolean_null=~Q(boolean=True))
    assert await BooleanFields.objects.order_by("id").values_list("boolean_null", flat=True) == [False, True]


def test_a_condition_value_takes_only_a_condition():
    with pytest.raises(TypeError, match="Q or Exists"):
        ConditionValue("desc")
