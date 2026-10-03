import pytest
import pytest_asyncio

from hare.contrib import test as hare_test
from hare.exceptions import ConfigurationError, FieldError, QueryError
from hare.query.expressions import Case, Exists, F, Q, When
from hare.query.functions import Count, Lower, Sum
from hare.sql import Order
from tests.testmodels import (
    CompositePkOrderedByPk,
    CompositePkThing,
    DefaultOrdered,
    DefaultOrderedByPk,
    DefaultOrderedDesc,
    DefaultOrderedInvalid,
    DefaultOrderedNullsLast,
    DocumentRevisionNote,
    Employee,
    Event,
    FKToDefaultOrdered,
    IntFields,
    OrderedByRelation,
    Tournament,
    VersionedDocument,
)

# ============================================================================
# TestOrderBy tests
# ============================================================================


@pytest.mark.asyncio
async def test_order_by(db):
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = await Tournament.objects.all().order_by("name")
    assert [t.name for t in tournaments] == ["1", "2"]


@pytest.mark.asyncio
async def test_order_by_reversed(db):
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = await Tournament.objects.all().order_by("-name")
    assert [t.name for t in tournaments] == ["2", "1"]


@pytest.mark.asyncio
async def test_order_by_related(db):
    tournament_first = await Tournament.objects.create(name="1")
    tournament_second = await Tournament.objects.create(name="2")
    await Event.objects.create(name="b", tournament=tournament_first)
    await Event.objects.create(name="a", tournament=tournament_second)

    tournaments = await Tournament.objects.all().order_by("events__name")
    assert [t.name for t in tournaments] == ["2", "1"]


@pytest.mark.asyncio
async def test_order_by_ambigious_field_name(db):
    tournament_first = await Tournament.objects.create(name="Tournament 1", desc="d1")
    tournament_second = await Tournament.objects.create(name="Tournament 2", desc="d2")

    event_third = await Event.objects.create(name="3", tournament=tournament_second)
    event_second = await Event.objects.create(name="2", tournament=tournament_first)
    event_first = await Event.objects.create(name="1", tournament=tournament_first)

    res = await Event.objects.all().order_by("tournament__name", "name")
    assert res == [event_first, event_second, event_third]


@pytest.mark.asyncio
async def test_order_by_related_reversed(db):
    tournament_first = await Tournament.objects.create(name="1")
    tournament_second = await Tournament.objects.create(name="2")
    await Event.objects.create(name="b", tournament=tournament_first)
    await Event.objects.create(name="a", tournament=tournament_second)

    tournaments = await Tournament.objects.all().order_by("-events__name")
    assert [t.name for t in tournaments] == ["1", "2"]


@pytest.mark.asyncio
async def test_order_by_relation(db):
    """A to-many relation orders by the related primary key, like Django."""
    tournament_first = await Tournament.objects.create(name="1")
    tournament_second = await Tournament.objects.create(name="2")
    first_event = await Event.objects.create(name="b", tournament=tournament_second)
    await Event.objects.create(name="a", tournament=tournament_first)
    tournaments = await Tournament.objects.all().order_by("events")
    assert [tournament.name for tournament in tournaments] == ["2", "1"]
    assert await Tournament.objects.all().order_by("-events").values_list("name", "events") == [
        ("1", first_event.event_id + 1),
        ("2", first_event.event_id),
    ]


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.asyncio
async def test_order_by_forward_fk_orders_by_its_key(db, descending):
    """Bug: order_by("tournament") raised FieldError("Filtering by relation is not possible")
    instead of ordering by the FK's own key column, which also broke after_cursor()/
    before_cursor()/cursor_values() on such an ordering."""
    tournaments = [await Tournament.objects.create(name=name) for name in ("b", "a", "c")]
    for tournament in (tournaments[2], tournaments[0], tournaments[1], tournaments[0]):
        await Event.objects.create(name="e", tournament=tournament)
    prefix = "-" if descending else ""

    by_relation = await Event.objects.all().order_by(f"{prefix}tournament", "pk").values_list("pk", flat=True)
    by_key = await Event.objects.all().order_by(f"{prefix}tournament_id", "pk").values_list("pk", flat=True)
    assert by_relation == by_key
    instances = await Event.objects.all().order_by(f"{prefix}tournament", "pk")
    assert [event.pk for event in instances] == by_key

    queryset = Event.objects.all().order_by(f"{prefix}tournament", "pk")
    first, second = instances[0], instances[1]
    assert queryset.cursor_values(first) == (first.tournament_id, first.pk)
    assert await queryset.after_cursor(*queryset.cursor_values(first)).values_list("pk", flat=True) == by_key[1:]
    assert await queryset.before_cursor(*queryset.cursor_values(second)).values_list("pk", flat=True) == by_key[:1]


@pytest.mark.asyncio
async def test_meta_ordering_by_forward_fk_orders_by_its_key(db):
    low = await DefaultOrdered.objects.create(one="a", second=1)
    high = await DefaultOrdered.objects.create(one="b", second=2)
    await OrderedByRelation.objects.create(link=low, value=2)
    await OrderedByRelation.objects.create(link=high, value=1)
    await OrderedByRelation.objects.create(link=low, value=1)

    rows = await OrderedByRelation.objects.all()
    assert [(row.link_id, row.value) for row in rows] == [(high.pk, 1), (low.pk, 1), (low.pk, 2)]


@pytest.mark.asyncio
async def test_order_by_composite_fk_orders_by_every_key_column(db):
    queryset = DocumentRevisionNote.objects.all().order_by("-document")
    assert queryset._orderings == [
        ("document_id", Order.DESC),
        ("document_version", Order.DESC),
    ]
    first_document = await VersionedDocument.objects.create(title="first")
    await DocumentRevisionNote.objects.create(document=first_document, note="n1")
    assert [note.note for note in await queryset] == ["n1"]


@pytest.mark.asyncio
async def test_order_by_unknown_field(db):
    with pytest.raises(FieldError):
        tournament_first = await Tournament.objects.create(name="1")
        await Event.objects.create(name="b", tournament=tournament_first)

        await Tournament.objects.all().order_by("something_else")


@pytest.mark.asyncio
async def test_order_by_pk(db):
    """ "pk" is a filter()/get() alias resolved specially, but order_by("pk") used to raise
    "Unknown field pk" for every model shape, contradicting iterator()'s own docstring
    recommendation to order by pk."""
    await Tournament.objects.create(id=2, name="B")
    await Tournament.objects.create(id=1, name="A")

    tournaments = await Tournament.objects.all().order_by("pk")
    assert [t.id for t in tournaments] == [1, 2]


@pytest.mark.asyncio
async def test_order_by_pk_reversed(db):
    await Tournament.objects.create(id=1, name="A")
    await Tournament.objects.create(id=2, name="B")

    tournaments = await Tournament.objects.all().order_by("-pk")
    assert [t.id for t in tournaments] == [2, 1]


@pytest.mark.asyncio
async def test_order_by_pk_composite_orders_by_every_component(db):
    for thing_id, revision in ((2, 1), (1, 2), (1, 1)):
        await CompositePkThing.objects.create(thing_id=thing_id, revision=revision, name=f"{thing_id}.{revision}")

    ascending = [(thing.thing_id, thing.revision) for thing in await CompositePkThing.objects.all().order_by("pk")]
    descending = [(thing.thing_id, thing.revision) for thing in await CompositePkThing.objects.all().order_by("-pk")]
    assert ascending == [(1, 1), (1, 2), (2, 1)]
    assert descending == [(2, 1), (1, 2), (1, 1)]


@pytest.mark.asyncio
async def test_default_ordering_by_pk(db):
    a = await DefaultOrderedByPk.objects.create(name="A")
    b = await DefaultOrderedByPk.objects.create(name="B")

    instances = await DefaultOrderedByPk.objects.all()
    assert [i.id for i in instances] == sorted([a.id, b.id], reverse=True)


@pytest.mark.asyncio
async def test_default_ordering_by_pk_composite_orders_by_every_component(db):
    for a, b in ((1, 1), (2, 1), (1, 2)):
        await CompositePkOrderedByPk.objects.create(a=a, b=b, name=f"{a}.{b}")

    instances = await CompositePkOrderedByPk.objects.all()
    assert [(instance.a, instance.b) for instance in instances] == [(1, 1), (1, 2), (2, 1)]


@pytest.mark.asyncio
async def test_order_by_aggregation(db):
    tournament_first = await Tournament.objects.create(name="1")
    tournament_second = await Tournament.objects.create(name="2")
    await Event.objects.create(name="b", tournament=tournament_first)
    await Event.objects.create(name="c", tournament=tournament_first)
    await Event.objects.create(name="a", tournament=tournament_second)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).order_by("events_count")
    assert [t.name for t in tournaments] == ["2", "1"]


@pytest.mark.asyncio
async def test_order_by_aggregation_reversed(db):
    tournament_first = await Tournament.objects.create(name="1")
    tournament_second = await Tournament.objects.create(name="2")
    await Event.objects.create(name="b", tournament=tournament_first)
    await Event.objects.create(name="c", tournament=tournament_first)
    await Event.objects.create(name="a", tournament=tournament_second)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).order_by("-events_count")
    assert [t.name for t in tournaments] == ["1", "2"]


@pytest.mark.asyncio
async def test_order_by_reserved_word_annotation(db):
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    reserved_words = ["order", "group", "limit", "offset", "where"]

    for word in reserved_words:
        tournaments = await Tournament.objects.annotate(**{word: Lower("name")}).order_by(word)
        assert [t.name for t in tournaments] == ["1", "2"]


@pytest.mark.asyncio
async def test_distinct_values_with_annotation(db):
    await Tournament.objects.create(name="3")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = (
        await Tournament.objects.annotate(
            name_orderable=Case(
                When(Q(name="1"), then="1"),
                When(Q(name="2"), then="2"),
                When(Q(name="3"), then="3"),
                default="-1",
            ),
        )
        .distinct()
        .order_by("name_orderable", "-created")
        .values("name", "name_orderable", "created")
    )
    assert [t["name"] for t in tournaments] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_distinct_all_with_annotation(db):
    await Tournament.objects.create(name="3")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = (
        await Tournament.objects.annotate(
            name_orderable=Case(
                When(Q(name="1"), then="1"),
                When(Q(name="2"), then="2"),
                When(Q(name="3"), then="3"),
                default="-1",
            ),
        )
        .distinct()
        .order_by("name_orderable", "-created")
    )
    assert [t.name for t in tournaments] == ["1", "2", "3"]


# ============================================================================
# TestDefaultOrdering tests
# ============================================================================


@pytest.mark.asyncio
async def test_default_order(db):
    await DefaultOrdered.objects.create(one="2", second=1)
    await DefaultOrdered.objects.create(one="1", second=1)

    instance_list = await DefaultOrdered.objects.all()
    assert [i.one for i in instance_list] == ["1", "2"]


@pytest.mark.asyncio
async def test_last_respects_meta_ordering(db):
    """last() used to skip Meta.ordering entirely and fall straight to pk DESC - on a model
    with Meta.ordering it returned a different row than the actual end of the sequence first()
    starts from (Meta.ordering = ["one", "second"] here: the last row is ("c", 3), not whichever
    was inserted last)."""
    for one, second in [("b", 1), ("a", 2), ("c", 3), ("a", 1)]:
        await DefaultOrdered.objects.create(one=one, second=second)

    first = await DefaultOrdered.objects.all().first()
    last = await DefaultOrdered.objects.all().last()

    assert (first.one, first.second) == ("a", 1)
    assert (last.one, last.second) == ("c", 3)


@pytest.mark.asyncio
async def test_default_order_desc(db):
    await DefaultOrderedDesc.objects.create(one="1", second=1)
    await DefaultOrderedDesc.objects.create(one="2", second=1)

    instance_list = await DefaultOrderedDesc.objects.all()
    assert [i.one for i in instance_list] == ["2", "1"]


@pytest.mark.asyncio
async def test_default_order_invalid(db):
    await DefaultOrderedInvalid.objects.create(one="1", second=1)
    await DefaultOrderedInvalid.objects.create(one="2", second=1)

    with pytest.raises(ConfigurationError):
        await DefaultOrderedInvalid.objects.all()


@pytest.mark.asyncio
async def test_default_order_survives_a_non_aggregate_annotation(db):
    """_apply_default_ordering() used to skip Meta.ordering for ANY annotation, not just one
    that forces an implicit GROUP BY - a plain non-aggregate annotation (F()/Exists()) selects
    exactly one row per real row, so Meta.ordering stays just as safe to apply as it is for an
    un-annotated query."""
    await DefaultOrdered.objects.create(one="b", second=1)
    await DefaultOrdered.objects.create(one="a", second=2)
    await DefaultOrdered.objects.create(one="c", second=3)
    await DefaultOrdered.objects.create(one="a", second=1)
    expected = [("a", 1), ("a", 2), ("b", 1), ("c", 3)]

    plain = [(o.one, o.second) for o in await DefaultOrdered.objects.all()]
    f_annotated = [(o.one, o.second) for o in await DefaultOrdered.objects.annotate(x=F("second") + 1)]
    exists_annotated = [
        (o.one, o.second)
        for o in await DefaultOrdered.objects.annotate(x=Exists(DefaultOrdered.objects.filter(one="a")))
    ]

    assert plain == expected
    assert f_annotated == expected
    assert exists_annotated == expected


@pytest.mark.asyncio
async def test_default_order_annotated_query(db):
    instance = await DefaultOrdered.objects.create(one="2", second=1)
    await FKToDefaultOrdered.objects.create(link=instance, value=10)
    await DefaultOrdered.objects.create(one="1", second=1)

    queryset = DefaultOrdered.objects.all().annotate(res=Sum("related__value"))._get_compiler()._get_execution_query()
    queryset._make_query()
    query = queryset.query.get_sql()
    assert "order by" not in query.lower()


@pytest.mark.asyncio
async def test_group_by_without_aggregate_ignores_meta_ordering(db):
    """A .group_by() query's rows are groups - Meta.ordering's columns aren't grouped, so an
    unordered one is ordered by the group key instead (PostgreSQL used to reject the query)."""
    for one, second in (("d", 1), ("a", 2), ("c", 1), ("e", 2), ("b", 3)):
        await DefaultOrdered.objects.create(one=one, second=second)
    grouped = DefaultOrdered.objects.all().group_by("second").values("second")

    assert await grouped.first() == {"second": 1}
    assert await grouped.last() == {"second": 3}
    assert sorted(row["second"] for row in await grouped) == [1, 2, 3]
    assert await grouped.values_list("second", flat=True).first() == 1
    assert await grouped.order_by("-second").values_list("second", flat=True) == [3, 2, 1]
    assert await DefaultOrdered.objects.all().group_by("second").annotate(n=Count("id")).values_list(
        "second", "n"
    ).first() == (1, 2)


# ============================================================================
# NULL placement: SQLite and PostgreSQL disagree on where NULLs sort by default, so a plain
# order_by() string is dialect-specific; F("field").asc()/.desc() with nulls_first/nulls_last
# fixes the position on every dialect.
# ============================================================================


@pytest_asyncio.fixture
async def scores_data(db):
    """intnum 1..5 with intnum_null 1, NULL, 2, 3, NULL."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)


@hare_test.requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_plain_order_by_null_placement_is_sqlite_default(scores_data):
    """Documents the unchanged dialect default: SQLite sorts NULL as the smallest value."""
    ascending = await IntFields.objects.all().order_by("intnum_null", "intnum").values_list("intnum", flat=True)
    descending = await IntFields.objects.all().order_by("-intnum_null", "intnum").values_list("intnum", flat=True)
    assert ascending == [2, 5, 1, 3, 4]
    assert descending == [4, 3, 1, 2, 5]


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_plain_order_by_null_placement_is_postgres_default(scores_data):
    """Documents the unchanged dialect default: PostgreSQL sorts NULL as the largest value."""
    ascending = await IntFields.objects.all().order_by("intnum_null", "intnum").values_list("intnum", flat=True)
    descending = await IntFields.objects.all().order_by("-intnum_null", "intnum").values_list("intnum", flat=True)
    assert ascending == [1, 3, 4, 2, 5]
    assert descending == [2, 5, 4, 3, 1]


@pytest.mark.parametrize(
    "ordering_kwargs,is_ascending,expected",
    [
        ({"nulls_first": True}, True, [2, 5, 1, 3, 4]),
        ({"nulls_last": True}, True, [1, 3, 4, 2, 5]),
        ({"nulls_first": True}, False, [2, 5, 4, 3, 1]),
        ({"nulls_last": True}, False, [4, 3, 1, 2, 5]),
    ],
)
@pytest.mark.asyncio
async def test_order_by_explicit_null_placement_is_the_same_on_every_dialect(
    scores_data, ordering_kwargs, is_ascending, expected
):
    field = F("intnum_null")
    ordering = field.asc(**ordering_kwargs) if is_ascending else field.desc(**ordering_kwargs)

    instances = await IntFields.objects.all().order_by(ordering, "intnum")
    assert [instance.intnum for instance in instances] == expected
    assert await IntFields.objects.all().order_by(ordering, "intnum").values_list("intnum", flat=True) == expected
    values = await IntFields.objects.all().order_by(ordering, "intnum").values("intnum")
    assert [row["intnum"] for row in values] == expected


@pytest.mark.asyncio
async def test_order_by_asc_desc_without_null_flags_is_a_plain_direction(scores_data):
    """F().asc()/.desc() with no flag leaves the NULL position to the dialect, like "field"/"-field"."""
    assert await IntFields.objects.all().order_by(
        F("intnum_null").asc(), "intnum"
    ) == await IntFields.objects.all().order_by("intnum_null", "intnum")
    assert await IntFields.objects.all().order_by(
        F("intnum_null").desc(), "intnum"
    ) == await IntFields.objects.all().order_by("-intnum_null", "intnum")


@pytest.mark.asyncio
async def test_order_by_conflicting_null_flags_raise(db):
    with pytest.raises(QueryError, match="mutually exclusive"):
        F("intnum_null").asc(nulls_first=True, nulls_last=True)
    with pytest.raises(QueryError, match="mutually exclusive"):
        F("intnum_null").desc(nulls_first=True, nulls_last=True)


@pytest.mark.asyncio
async def test_order_by_rejects_an_item_that_is_neither_string_nor_ordering(db):
    with pytest.raises(QueryError, match="must be a field name string or an Ordering"):
        IntFields.objects.all().order_by(F("intnum_null"))


@pytest.mark.asyncio
async def test_order_by_explicit_null_placement_composite_with_duplicates(db):
    """Duplicate values plus NULLs: the second key breaks ties inside every group, NULL group included."""
    for number, score in [(1, 3), (2, None), (3, 3), (4, None), (5, 1)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)

    nulls_last = await IntFields.objects.all().order_by(F("intnum_null").desc(nulls_last=True), F("intnum").desc())
    assert [instance.intnum for instance in nulls_last] == [3, 1, 5, 4, 2]
    nulls_first = await IntFields.objects.all().order_by(F("intnum_null").asc(nulls_first=True), F("intnum").desc())
    assert [instance.intnum for instance in nulls_first] == [4, 2, 5, 3, 1]


@pytest.mark.parametrize("nulls_kwargs", [{"nulls_first": True}, {"nulls_last": True}])
@pytest.mark.asyncio
async def test_order_by_explicit_null_placement_single_null_and_all_null(db, nulls_kwargs):
    await IntFields.objects.create(intnum=1, intnum_null=5)
    await IntFields.objects.create(intnum=2, intnum_null=None)
    ordered = (
        await IntFields.objects.all().order_by(F("intnum_null").asc(**nulls_kwargs)).values_list("intnum", flat=True)
    )
    assert ordered == ([2, 1] if "nulls_first" in nulls_kwargs else [1, 2])

    await IntFields.objects.filter(intnum=1).update(intnum_null=None)
    ordered = (
        await IntFields.objects.all()
        .order_by(F("intnum_null").asc(**nulls_kwargs), "intnum")
        .values_list("intnum", flat=True)
    )
    assert ordered == [1, 2]


@pytest.mark.parametrize(
    "ordering_factory,expected",
    [
        (lambda: F("manager__name").asc(nulls_first=True), ["Boss", "CEO", "Worker A", "Worker B"]),
        (lambda: F("manager__name").asc(nulls_last=True), ["Worker A", "Worker B", "Boss", "CEO"]),
        (lambda: F("manager__name").desc(nulls_first=True), ["Boss", "CEO", "Worker A", "Worker B"]),
        (lambda: F("manager__name").desc(nulls_last=True), ["Worker A", "Worker B", "Boss", "CEO"]),
    ],
)
@pytest.mark.asyncio
async def test_order_by_related_field_null_from_left_join(db, ordering_factory, expected):
    """A related-field ordering meets NULLs through the LEFT JOIN even though Employee.name itself is not
    nullable - the placement must apply to them too."""
    await Employee.objects.create(name="CEO")
    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Worker A", manager=boss)
    await Employee.objects.create(name="Worker B", manager=boss)

    employees = await Employee.objects.all().order_by(ordering_factory(), "name")
    assert [employee.name for employee in employees] == expected


@pytest.mark.asyncio
async def test_order_by_annotation_with_explicit_null_placement(scores_data):
    queryset = IntFields.objects.annotate(doubled=F("intnum_null") * 2)
    nulls_first = await queryset.order_by(F("doubled").desc(nulls_first=True), "intnum")
    assert [instance.intnum for instance in nulls_first] == [2, 5, 4, 3, 1]
    nulls_last = await queryset.order_by(F("doubled").asc(nulls_last=True), "intnum")
    assert [instance.intnum for instance in nulls_last] == [1, 3, 4, 2, 5]


@pytest.mark.asyncio
async def test_first_and_last_respect_explicit_null_placement(scores_data):
    """last() reverses the ordering exactly: the direction AND the explicit NULL placement flip."""
    nulls_first_ordering = F("intnum_null").asc(nulls_first=True)
    assert (await IntFields.objects.all().order_by(nulls_first_ordering, "intnum").first()).intnum == 2
    assert (await IntFields.objects.all().order_by(nulls_first_ordering, "intnum").last()).intnum == 4

    nulls_last_ordering = F("intnum_null").asc(nulls_last=True)
    assert (await IntFields.objects.all().order_by(nulls_last_ordering, "intnum").first()).intnum == 1
    assert (await IntFields.objects.all().order_by(nulls_last_ordering, "intnum").last()).intnum == 5


@pytest.mark.parametrize("nulls_kwargs", [{"nulls_first": True}, {"nulls_last": True}])
@pytest.mark.asyncio
async def test_last_is_the_exact_reverse_of_the_full_ordering(scores_data, nulls_kwargs):
    ordering = F("intnum_null").desc(**nulls_kwargs)
    full = [instance.intnum for instance in await IntFields.objects.all().order_by(ordering, "intnum")]
    assert (await IntFields.objects.all().order_by(ordering, "intnum").last()).intnum == full[-1]
    plain_full = [instance.intnum for instance in await IntFields.objects.all().order_by("intnum_null", "intnum")]
    assert (await IntFields.objects.all().order_by("intnum_null", "intnum").last()).intnum == plain_full[-1]


@pytest.mark.parametrize(
    "order,reversed_order",
    [
        (Order.ASC, Order.DESC),
        (Order.DESC, Order.ASC),
        (Order.ASC_NULLS_FIRST, Order.DESC_NULLS_LAST),
        (Order.ASC_NULLS_LAST, Order.DESC_NULLS_FIRST),
        (Order.DESC_NULLS_FIRST, Order.ASC_NULLS_LAST),
        (Order.DESC_NULLS_LAST, Order.ASC_NULLS_FIRST),
    ],
)
def test_order_reversal_inverts_direction_and_explicit_null_placement(order, reversed_order):
    assert order.get_reversed() == reversed_order
    assert order.get_reversed().get_reversed() == order
    assert Order.build(order.is_ascending, order.nulls_first) == order


def test_order_renders_the_sql_keywords():
    assert str(Order.ASC_NULLS_LAST) == "ASC NULLS LAST"
    assert str(Order.DESC_NULLS_FIRST) == "DESC NULLS FIRST"
    assert Order.ASC.nulls_first is None


@pytest.mark.asyncio
async def test_meta_ordering_accepts_an_ordering_with_null_placement(db):
    """Meta.ordering = [F("score").asc(nulls_last=True), "label"] - NULLs last on every dialect."""
    for label, score in [("a", 3), ("b", None), ("c", 1), ("d", None)]:
        await DefaultOrderedNullsLast.objects.create(label=label, score=score)

    assert [row.label for row in await DefaultOrderedNullsLast.objects.all()] == ["c", "a", "b", "d"]
    assert (await DefaultOrderedNullsLast.objects.all().first()).label == "c"
    assert (await DefaultOrderedNullsLast.objects.all().last()).label == "d"
    assert [row.label for row in await DefaultOrderedNullsLast.objects.all().order_by("label")] == ["a", "b", "c", "d"]
