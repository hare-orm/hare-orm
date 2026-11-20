"""group_by(Rollup(...) | Cube(...) | GroupingSets(...)) - several groupings of one values().annotate()
query, Grouping() telling a grouping's NULL from a NULL value - and the database without them."""

from __future__ import annotations

from collections import Counter

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError, QueryError, UnSupportedError
from hare.query.expressions import F
from hare.query.functions import Count, Grouping, Sum
from hare.query.grouping import Cube, GroupingSets, Rollup
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Event, IntFields, RecursiveCompositeNode, Tournament

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
        .order_by(*ORDERING, "left_out", "both")
    )
    # Row 5's NULL is a value (left_out 0); the subtotal's NULL is the grouping's (left_out 1).
    assert [(row["intnum_null"], row["total"], row["left_out"], row["both"]) for row in rows] == [
        (None, 5, 0, 0),
        (None, 5, 1, 1),
        (None, 5, 1, 3),
    ]


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
async def test_summaries_keep_the_groups_when_ordering_by_a_grouping_alias(db):
    await create_rows()
    groups = (
        IntFields.objects.values("intnum")
        .annotate(mask=Grouping("intnum"), total=Sum("id"))
        .group_by(Rollup("intnum"))
        .order_by("mask", "intnum")
    )

    assert await groups.count() == 3
    assert await groups.exists()
    assert await groups[1:2].count() == 1
    assert not await groups[3:].exists()


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
@pytest.mark.parametrize("grouping_set", [Rollup, Cube])
async def test_a_relation_to_a_composite_key_is_grouped_whole(db, grouping_set):
    root = await RecursiveCompositeNode.objects.create(a=1, b=1, name="root")
    first = await RecursiveCompositeNode.objects.create(a=1, b=2, name="first", parent=root)
    await RecursiveCompositeNode.objects.create(a=2, b=1, name="second", parent=root)
    await RecursiveCompositeNode.objects.create(a=2, b=2, name="third", parent=first)
    rows = (
        await RecursiveCompositeNode.objects.values("parent")
        .annotate(nodes=Count("name"))
        .group_by(grouping_set("parent"))
    )
    # The key's columns are one unit - no grouping takes one of them alone.
    assert sorted((row["nodes"], str(row["parent"])) for row in rows) == [
        (1, "(1, 2)"),
        (1, "None"),
        (2, "(1, 1)"),
        (4, "None"),
    ]


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("grouping_type", [Rollup, Cube, GroupingSets])
@pytest.mark.parametrize("key_source", ["field", "annotation", "alias"])
async def test_grouping_has_one_bit_per_logical_composite_field(db, grouping_type, key_source):
    root = await RecursiveCompositeNode.objects.create(a=1, b=1, name="root")
    await RecursiveCompositeNode.objects.create(a=1, b=2, name="first", parent=root)
    await RecursiveCompositeNode.objects.create(a=2, b=1, name="second", parent=root)
    data = [((1, 1), None), ((1, 2), (1, 1)), ((2, 1), (1, 1))]
    if grouping_type is Rollup:
        subsets = [(0, 1), (0,), ()]
    elif grouping_type is Cube:
        subsets = [(0, 1), (0,), (1,), ()]
    else:
        subsets = [(0, 1), (1,), ()]
    base = RecursiveCompositeNode.objects.all()
    if key_source == "annotation":
        base = base.annotate(key=F("pk"))
    elif key_source == "alias":
        base = base.alias(key=F("pk"))
    names = ("pk" if key_source == "field" else "key", "parent")
    grouping = (
        GroupingSets(*(tuple(names[index] for index in subset) for subset in subsets))
        if grouping_type is GroupingSets
        else grouping_type(*names)
    )
    expected = Counter()
    for subset in subsets:
        groups = Counter(tuple(row[index] if index in subset else None for index in range(2)) for row in data)
        mask = sum(1 << (1 - index) for index in range(2) if index not in subset)
        expected.update((key[0], key[1], count, mask) for key, count in groups.items())
    rows = await base.values(*names).annotate(nodes=Count("name"), mask=Grouping(*names)).group_by(grouping)
    assert Counter((row[names[0]], row["parent"], row["nodes"], row["mask"]) for row in rows) == expected
    assert all(type(row["mask"]) is int for row in rows)


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("grouping_type", [Rollup, Cube, GroupingSets])
@pytest.mark.parametrize("grouping_first", [False, True])
async def test_grouping_reuses_parameterized_annotation_inputs(db, grouping_type, grouping_first):
    await create_rows()
    names = ("intnum", "bucket")
    if grouping_type is Rollup:
        subsets = [(0, 1), (0,), ()]
    elif grouping_type is Cube:
        subsets = [(0, 1), (0,), (1,), ()]
    else:
        subsets = [(0, 1), (1,), ()]
    grouping = (
        GroupingSets(*(tuple(names[index] for index in subset) for subset in subsets))
        if grouping_type is GroupingSets
        else grouping_type(*names)
    )
    for delta in (5, 9, 5):
        annotations = {"bucket": F("intnum_null") + delta, "mask": Grouping(*names)}
        if grouping_first:
            annotations = dict(reversed(annotations.items()))
        rows = await (
            IntFields.objects.annotate(**annotations)
            .values(*names, "mask")
            .annotate(nodes=Count("id"))
            .group_by(grouping)
        )
        data = [(1, 10 + delta), (1, 20 + delta), (2, 10 + delta), (2, 10 + delta)]
        expected = Counter()
        for subset in subsets:
            groups = Counter(tuple(row[index] if index in subset else None for index in range(2)) for row in data)
            mask = sum(1 << (1 - index) for index in range(2) if index not in subset)
            expected.update((key[0], key[1], count, mask) for key, count in groups.items())
        assert Counter((row["intnum"], row["bucket"], row["nodes"], row["mask"]) for row in rows) == expected


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mutual", [False, True])
async def test_grouping_annotation_cycles_keep_the_declaration_error(db, mutual):
    annotations = {"flag": Grouping("other" if mutual else "flag")}
    if mutual:
        annotations["other"] = Grouping("flag")
    with pytest.raises(QueryError, match="Circular annotation reference"):
        await IntFields.objects.annotate(**annotations).values("flag").group_by("intnum")


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
def test_a_wrong_grouping_is_refused(make, message, db):
    with pytest.raises(QueryError, match=message):
        make()


@requires_features(supports_grouping_sets=True)
@pytest.mark.asyncio
async def test_a_grouping_of_a_field_the_query_doesnt_group_by_is_the_databases_to_refuse(db):
    """GROUPING() of a field outside GROUP BY is refused by the database itself; one of a plainly
    grouped field is 0."""
    await IntFields.objects.create(intnum=1, intnum_null=1)
    with pytest.raises(OperationalError, match="GROUPING"):
        async with Transactions.atomic():
            await IntFields.objects.annotate(total=Sum("id"), flag=Grouping("intnum")).values("total", "flag")
    with pytest.raises(OperationalError, match="GROUPING"):
        async with Transactions.atomic():
            await (
                IntFields.objects.values("intnum_null")
                .annotate(total=Sum("id"), flag=Grouping("intnum"))
                .group_by(Rollup("intnum_null"))
            )
    rows = (
        await IntFields.objects.values("intnum", "intnum_null")
        .annotate(total=Sum("id"), flag=Grouping("intnum"))
        .group_by("intnum", Rollup("intnum_null"))
    )
    assert {row["flag"] for row in rows} == {0}
