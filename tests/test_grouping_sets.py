"""group_by(Rollup(...) | Cube(...) | GroupingSets(...)) - several groupings of one values().annotate()
query, Grouping() telling a grouping's NULL from a NULL value - and the database without them."""

from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import F
from hare.query.functions import Count, Grouping, Sum
from hare.query.grouping import Cube, GroupingSets, Rollup
from tests.testmodels import Event, IntFields, Tournament

ORDERING = (F("intnum").asc(nulls_last=True), F("intnum_null").asc(nulls_last=True))


async def create_rows() -> None:
    for row_id, intnum, intnum_null in [(1, 1, 10), (2, 1, 20), (3, 2, 10), (4, 2, 10)]:
        await IntFields.objects.create(id=row_id, intnum=intnum, intnum_null=intnum_null)


async def get_groups(*group_by) -> list[tuple]:
    rows = (
        await IntFields.objects.values("intnum", "intnum_null")
        .annotate(total=Sum("id"))
        .group_by(*group_by)
        .order_by(*ORDERING)
    )
    return [(row["intnum"], row["intnum_null"], row["total"]) for row in rows]


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        (
            (Rollup("intnum", "intnum_null"),),
            [(1, 10, 1), (1, 20, 2), (1, None, 3), (2, 10, 7), (2, None, 7), (None, None, 10)],
        ),
        (
            (Cube("intnum", "intnum_null"),),
            [
                (1, 10, 1),
                (1, 20, 2),
                (1, None, 3),
                (2, 10, 7),
                (2, None, 7),
                (None, 10, 8),
                (None, 20, 2),
                (None, None, 10),
            ],
        ),
        (
            (GroupingSets(("intnum",), "intnum_null"),),
            [(1, None, 3), (2, None, 7), (None, 10, 8), (None, 20, 2)],
        ),
        (
            (GroupingSets(("intnum", "intnum_null"), ()),),
            [(1, 10, 1), (1, 20, 2), (2, 10, 7), (None, None, 10)],
        ),
        (
            ("intnum", Rollup("intnum_null")),
            [(1, 10, 1), (1, 20, 2), (1, None, 3), (2, 10, 7), (2, None, 7)],
        ),
    ],
    ids=["rollup", "cube", "grouping sets", "grouping sets with a total", "a field beside a rollup"],
)
async def test_the_groups_of_each_grouping(db, group_by, expected):
    await create_rows()
    assert await get_groups(*group_by) == expected


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
async def test_grouping_tells_a_grouping_null_from_a_null_value(db):
    await create_rows()
    await IntFields.objects.create(id=5, intnum=3, intnum_null=None)
    rows = (
        await IntFields.objects.values("intnum", "intnum_null")
        .annotate(total=Sum("id"), left_out=Grouping("intnum_null"), both=Grouping("intnum", "intnum_null"))
        .group_by(Rollup("intnum", "intnum_null"))
        .filter(intnum=3)
        .order_by(*ORDERING, "left_out")
    )
    # Row 5's NULL is a value (left_out 0); the subtotal's NULL is the grouping's (left_out 1).
    assert [(row["intnum_null"], row["total"], row["left_out"], row["both"]) for row in rows] == [
        (None, 5, 0, 0),
        (None, 5, 1, 1),
        (None, 5, 1, 3),
    ]


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
async def test_an_annotation_in_a_rollup(db):
    await create_rows()
    rows = (
        await IntFields.objects.annotate(bucket=F("intnum") * 100)
        .values("bucket")
        .annotate(total=Sum("id"))
        .group_by(Rollup("bucket"))
        .order_by(F("bucket").asc(nulls_last=True))
    )
    assert [(row["bucket"], row["total"]) for row in rows] == [(100, 3), (200, 7), (None, 10)]


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
async def test_a_related_field_in_a_rollup(db):
    first = await Tournament.objects.create(id=1, name="First")
    second = await Tournament.objects.create(id=2, name="Second")
    for event_id, tournament in enumerate([first, first, second], start=1):
        await Event.objects.create(event_id=event_id, tournament=tournament, name=f"event {event_id}")
    rows = (
        await Event.objects.values("tournament__name")
        .annotate(events=Count("event_id"))
        .group_by(Rollup("tournament__name"))
        .order_by(F("tournament__name").asc(nulls_last=True))
    )
    assert [(row["tournament__name"], row["events"]) for row in rows] == [("First", 2), ("Second", 1), (None, 3)]


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
async def test_aggregate_over_groupings_is_refused(db):
    await create_rows()
    with pytest.raises(QueryError, match="would count rows once per grouping"):
        await (
            IntFields.objects.values("intnum")
            .annotate(total=Sum("id"))
            .group_by(Rollup("intnum"))
            .aggregate(all=Sum("total"))
        )


@requires_features(supports_grouping_sets=False)
@pytest.mark.asyncio
async def test_a_database_without_grouping_sets_refuses_them(db):
    await create_rows()
    with pytest.raises(UnSupportedError, match="needs GROUP BY grouping sets"):
        await get_groups(Rollup("intnum", "intnum_null"))
    with pytest.raises(UnSupportedError, match="Grouping\\(\\) needs GROUPING\\(\\)"):
        await IntFields.objects.values("intnum").annotate(flag=Grouping("intnum")).group_by("intnum")


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Rollup(), "Rollup\\(\\) takes field names"),
        (lambda: Cube("a", ""), "Cube\\(\\) takes field names"),
        (lambda: GroupingSets(), "takes the groupings, got none"),
        (lambda: GroupingSets(5), "takes field names or sequences of them"),
        (lambda: GroupingSets((), ()), "needs a field in one of its groupings"),
        (lambda: Grouping(), "Grouping\\(\\) takes the grouped field names"),
        (
            lambda: IntFields.objects.group_by(Rollup("intnum"), Cube("intnum")),
            "takes one Rollup, Cube or GroupingSets",
        ),
    ],
)
def test_a_wrong_grouping_is_refused(make, message):
    with pytest.raises(QueryError, match=message):
        make()
