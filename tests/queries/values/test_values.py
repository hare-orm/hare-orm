import pytest

from hare.contrib import test
from hare.contrib.test.conditions.in_condition import In
from hare.exceptions import FieldError
from hare.query.expressions import Case, F, Function, Q, When
from hare.query.functions import Length, Trim
from hare.sql import CustomFunction
from tests.shared_models.testmodels import CompositePkThing, Event, Team, Tournament

# .values() and .values_list() - `as_row` turns the field names and a row's values into the row the
# method returns.
VALUES_METHODS = [
    pytest.param("values", lambda fields, row: dict(zip(fields, row, strict=True)), id="values"),
    pytest.param("values_list", lambda fields, row: row, id="values_list"),
]


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.asyncio
async def test_limit_zero_returns_empty(db, method_name, as_row):
    """limit(0) must actually apply LIMIT 0, not be dropped as falsy (regression guard)."""
    await Tournament.objects.create(name="New Tournament")

    result = await getattr(Tournament.objects.all().limit(0), method_name)("name")
    assert result == []


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.asyncio
async def test_pk_alias(db, method_name, as_row):
    """ "pk" is a .filter()/.get()/.order_by() alias for the real pk field name, but was never
    registered in model._meta.fields itself - .values()/.values_list() raised "Unknown field pk"
    for it instead of resolving it the same way those other call sites already do."""
    tournament = await Tournament.objects.create(name="New Tournament")

    assert await getattr(Tournament.objects.all(), method_name)("pk") == [as_row(("pk",), (tournament.id,))]


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.asyncio
async def test_rejects_prefetch_related(db, method_name, as_row):
    """.values() used to silently drop a prior prefetch_related() call - the result is plain
    dicts, not model instances, so there was nothing to actually attach the prefetched relation
    to; the prefetch queries just never ran and the caller got no signal anything was wrong."""
    with pytest.raises(ValueError, match="prefetch_related"):
        getattr(Tournament.objects.all().prefetch_related("events"), method_name)("name")


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.parametrize(
    ("model_class", "filter_kwargs", "fields", "get_values"),
    [
        pytest.param(
            Event,
            {"name": "Test"},
            ("name", "tournament__name"),
            lambda tournament, event, team: ("Test", "New Tournament"),
            id="related_fk",
        ),
        pytest.param(
            Tournament,
            {"name": "New Tournament"},
            ("name", "events__name"),
            lambda tournament, event, team: ("New Tournament", "Test"),
            id="related_rfk",
        ),
        pytest.param(
            Event,
            {"name": "Test"},
            ("name", "participants__name"),
            lambda tournament, event, team: ("Test", "Some Team"),
            id="related_m2m",
        ),
        pytest.param(
            Event,
            {"name": "Test"},
            ("name", "tournament"),
            lambda tournament, event, team: ("Test", tournament.id),
            id="related_fk_itself",
        ),
        pytest.param(
            Tournament,
            {"name": "New Tournament"},
            ("name", "events"),
            lambda tournament, event, team: ("New Tournament", event.event_id),
            id="related_rfk_itself",
        ),
        pytest.param(
            Event,
            {"name": "Test"},
            ("name", "participants"),
            lambda tournament, event, team: ("Test", team.id),
            id="related_m2m_itself",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_related_field(db, model_class, filter_kwargs, fields, get_values, method_name, as_row):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)
    team = await Team.objects.create(name="Some Team")
    await event.participants.add(team)

    rows = await getattr(model_class.objects.filter(**filter_kwargs), method_name)(*fields)
    assert rows == [as_row(fields, get_values(tournament, event, team))]


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.asyncio
async def test_related_rfk_reuse_query(db, method_name, as_row):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    fields = ("name", "events__name")
    query = getattr(Tournament.objects.filter(name="New Tournament"), method_name)(*fields)
    tournament2 = await query
    assert tournament2[0] == as_row(fields, ("New Tournament", "Test"))

    tournament2 = await query
    assert tournament2[0] == as_row(fields, ("New Tournament", "Test"))


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.parametrize(
    ("field_name", "message"),
    [
        pytest.param("neem", r"Event.objects.{method_name}\('neem'\): Event has no field 'neem'", id="bad_key"),
        pytest.param(
            "tournament__neem",
            "Unknown field 'tournament__neem': Tournament has no field 'neem'",
            id="related_bad_key",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_key(db, field_name, message, method_name, as_row):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    with pytest.raises(FieldError, match=message.format(method_name=method_name)):
        await getattr(Event.objects.filter(name="Test"), method_name)("name", field_name)


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.parametrize(
    ("function_class", "rows"),
    [
        pytest.param(Length, [("Championship", 12), ("Super Bowl", 10)], id="annotations_length"),
        pytest.param(Trim, [("  x", "x"), (" y ", "y")], id="annotations_trim"),
    ],
)
@pytest.mark.asyncio
async def test_annotations(db, function_class, rows, method_name, as_row):
    for name, _ in rows:
        await Tournament.objects.create(name=name)

    fields = ("name", "name_changed")
    tournaments = await getattr(Tournament.objects.annotate(name_changed=function_class("name")), method_name)(*fields)
    expected = [as_row(fields, row) for row in rows]
    assert sorted(tournaments, key=str) == sorted(expected, key=str)


@pytest.mark.asyncio
async def test_values_kwarg_expression_matches_annotate_then_values(db):
    """A .values() kwarg whose value is an Expression (not a field name string) must produce
    the exact same result as .annotate(key=expression).values(key) - it's the same mechanism,
    just built without a separate .annotate() call."""
    await Tournament.objects.create(id=1, name="T1")

    via_values = await Tournament.objects.all().values(shifted=F("id") + 1)
    via_annotate = await Tournament.objects.all().annotate(shifted=F("id") + 1).values("shifted")
    assert via_values == via_annotate == [{"shifted": 2}]


@pytest.mark.asyncio
async def test_values_list_kwarg_expression_matches_annotate_then_values_list(db):
    await Tournament.objects.create(id=1, name="T1")

    via_values_list = await Tournament.objects.all().values_list(shifted=F("id") + 1)
    via_annotate = await Tournament.objects.all().annotate(shifted=F("id") + 1).values_list("shifted")
    assert via_values_list == via_annotate == [(2,)]

    flat = await Tournament.objects.all().values_list(shifted=F("id") + 1, flat=True)
    assert flat == [2]


@pytest.mark.asyncio
async def test_values_kwarg_expression_combined_with_positional_field_names(db):
    await Tournament.objects.create(id=1, name="T1")

    rows = await Tournament.objects.all().values("name", label=F("id") + 100)
    assert rows == [{"name": "T1", "label": 101}]

    tuples = await Tournament.objects.all().values_list("name", label=F("id") + 100)
    assert tuples == [("T1", 101)]


@pytest.mark.asyncio
async def test_values_kwarg_expression_does_not_leak_into_original_queryset(db):
    """A .values(key=expression) call must build its own annotations dict - the ORIGINAL
    QuerySet's own annotations (and any other query later derived from it) must not see it,
    mirroring add_field_to_select_query()'s identical concern for a renamed field lookup."""
    await Tournament.objects.create(id=1, name="T1")

    qs = Tournament.objects.all()
    await qs.values(shifted=F("id") + 1)
    assert qs._annotations == {}

    # A second .values() call from the same base queryset, with a DIFFERENT expression under
    # the same key, must not see the first call's expression either.
    other = await qs.values(shifted=F("id") + 1000)
    assert other == [{"shifted": 1001}]


@pytest.mark.asyncio
async def test_values_kwarg_expression_combined_with_filter_and_order_by(db):
    await Tournament.objects.create(id=1, name="T1")
    await Tournament.objects.create(id=2, name="T2")

    rows = await Tournament.objects.filter(id__gte=1).order_by("-id").values(doubled=F("id") * 2)
    assert rows == [{"doubled": 4}, {"doubled": 2}]


@test.requires_features(dialect=In("sqlite"))
@pytest.mark.asyncio
async def test_values_with_custom_function(db):
    class TruncMonth(Function):
        database_function = CustomFunction("DATE_FORMAT", ["name", "dt_format"])

    sql = Tournament.objects.all().annotate(date=TruncMonth("created", "%Y-%m-%d")).values("date").sql()
    assert sql == 'SELECT DATE_FORMAT("created",?) "date" FROM "tournament"'


@pytest.mark.parametrize(("method_name", "as_row"), VALUES_METHODS)
@pytest.mark.asyncio
async def test_order_by_annotation_not_in_values(db, method_name, as_row):
    await Tournament.objects.create(name="2")
    await Tournament.objects.create(name="3")
    await Tournament.objects.create(name="1")

    tournaments = await getattr(
        Tournament.objects.annotate(
            name_orderable=Case(
                When(Q(name="1"), then="a"),
                When(Q(name="2"), then="b"),
                When(Q(name="3"), then="c"),
                default="z",
            )
        ).order_by("name_orderable"),
        method_name,
    )("name")
    assert tournaments == [as_row(("name",), (name,)) for name in ("1", "2", "3")]


@pytest.mark.asyncio
async def test_a_renamed_composite_primary_key_is_filtered_ordered_and_selected_by_its_new_name(db):
    """values(key="pk") of a composite key made the alias F("pk") - a filter, an ordering or a selection by
    the new name raised FieldError: there is no field pk."""
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="a")
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="b")
    query = CompositePkThing.objects.values(key="pk")
    assert await query.filter(key=(1, 2)) == [{"key": (1, 2)}]
    assert await query.filter(key__in=[(1, 1)]) == [{"key": (1, 1)}]
    assert await query.order_by("-key") == [{"key": (1, 2)}, {"key": (1, 1)}]
    assert await query.order_by("key").values("key") == [{"key": (1, 1)}, {"key": (1, 2)}]
