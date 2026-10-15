import pytest

from hare.contrib import test
from hare.contrib.test.conditions.in_condition import In
from hare.exceptions import FieldError
from hare.query.expressions import Case, F, Function, Q, When
from hare.query.functions import Length, Trim
from hare.sql import CustomFunction
from tests.testmodels import Event, Team, Tournament


@pytest.mark.asyncio
async def test_values_limit_zero_returns_empty(db):
    """limit(0) must actually apply LIMIT 0, not be dropped as falsy (regression guard)."""
    await Tournament.objects.create(name="New Tournament")

    result = await Tournament.objects.all().limit(0).values("name")
    assert result == []


@pytest.mark.asyncio
async def test_values_list_limit_zero_returns_empty(db):
    """limit(0) must actually apply LIMIT 0, not be dropped as falsy (regression guard)."""
    await Tournament.objects.create(name="New Tournament")

    result = await Tournament.objects.all().limit(0).values_list("name")
    assert result == []


@pytest.mark.asyncio
async def test_values_pk_alias(db):
    """ "pk" is a .filter()/.get()/.order_by() alias for the real pk field name, but was never
    registered in model._meta.fields itself - .values()/.values_list() raised "Unknown field pk"
    for it instead of resolving it the same way those other call sites already do."""
    tournament = await Tournament.objects.create(name="New Tournament")

    assert await Tournament.objects.all().values("pk") == [{"pk": tournament.id}]


@pytest.mark.asyncio
async def test_values_list_pk_alias(db):
    tournament = await Tournament.objects.create(name="New Tournament")

    assert await Tournament.objects.all().values_list("pk", flat=True) == [tournament.id]


@pytest.mark.asyncio
async def test_values_rejects_prefetch_related(db):
    """.values() used to silently drop a prior prefetch_related() call - the result is plain
    dicts, not model instances, so there was nothing to actually attach the prefetched relation
    to; the prefetch queries just never ran and the caller got no signal anything was wrong."""
    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().prefetch_related("events").values("name")


@pytest.mark.asyncio
async def test_values_list_rejects_prefetch_related(db):
    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().prefetch_related("events").values_list("name")


@pytest.mark.asyncio
async def test_values_related_fk(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    event2 = await Event.objects.filter(name="Test").values("name", "tournament__name")
    assert event2[0] == {"name": "Test", "tournament__name": "New Tournament"}


@pytest.mark.asyncio
async def test_values_list_related_fk(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    event2 = await Event.objects.filter(name="Test").values_list("name", "tournament__name")
    assert event2[0] == ("Test", "New Tournament")


@pytest.mark.asyncio
async def test_values_related_rfk(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    tournament2 = await Tournament.objects.filter(name="New Tournament").values("name", "events__name")
    assert tournament2[0] == {"name": "New Tournament", "events__name": "Test"}


@pytest.mark.asyncio
async def test_values_related_rfk_reuse_query(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    query = Tournament.objects.filter(name="New Tournament").values("name", "events__name")
    tournament2 = await query
    assert tournament2[0] == {"name": "New Tournament", "events__name": "Test"}

    tournament2 = await query
    assert tournament2[0] == {"name": "New Tournament", "events__name": "Test"}


@pytest.mark.asyncio
async def test_values_list_related_rfk(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    tournament2 = await Tournament.objects.filter(name="New Tournament").values_list("name", "events__name")
    assert tournament2[0] == ("New Tournament", "Test")


@pytest.mark.asyncio
async def test_values_list_related_rfk_reuse_query(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    query = Tournament.objects.filter(name="New Tournament").values_list("name", "events__name")
    tournament2 = await query
    assert tournament2[0] == ("New Tournament", "Test")

    tournament2 = await query
    assert tournament2[0] == ("New Tournament", "Test")


@pytest.mark.asyncio
async def test_values_related_m2m(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)
    team = await Team.objects.create(name="Some Team")
    await event.participants.add(team)

    tournament2 = await Event.objects.filter(name="Test").values("name", "participants__name")
    assert tournament2[0] == {"name": "Test", "participants__name": "Some Team"}


@pytest.mark.asyncio
async def test_values_list_related_m2m(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)
    team = await Team.objects.create(name="Some Team")
    await event.participants.add(team)

    tournament2 = await Event.objects.filter(name="Test").values_list("name", "participants__name")
    assert tournament2[0] == ("Test", "Some Team")


@pytest.mark.asyncio
async def test_values_related_fk_itself(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    assert await Event.objects.filter(name="Test").values("name", "tournament") == [
        {"name": "Test", "tournament": tournament.id}
    ]


@pytest.mark.asyncio
async def test_values_list_related_fk_itself(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    assert await Event.objects.filter(name="Test").values_list("name", "tournament") == [("Test", tournament.id)]


@pytest.mark.asyncio
async def test_values_related_rfk_itself(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)

    assert await Tournament.objects.filter(name="New Tournament").values("name", "events") == [
        {"name": "New Tournament", "events": event.event_id}
    ]


@pytest.mark.asyncio
async def test_values_list_related_rfk_itself(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)

    assert await Tournament.objects.filter(name="New Tournament").values_list("name", "events") == [
        ("New Tournament", event.event_id)
    ]


@pytest.mark.asyncio
async def test_values_related_m2m_itself(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)
    team = await Team.objects.create(name="Some Team")
    await event.participants.add(team)

    assert await Event.objects.filter(name="Test").values("name", "participants") == [
        {"name": "Test", "participants": team.id}
    ]


@pytest.mark.asyncio
async def test_values_list_related_m2m_itself(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    event = await Event.objects.create(name="Test", tournament_id=tournament.id)
    team = await Team.objects.create(name="Some Team")
    await event.participants.add(team)

    assert await Event.objects.filter(name="Test").values_list("name", "participants") == [("Test", team.id)]


@pytest.mark.asyncio
async def test_values_bad_key(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    with pytest.raises(FieldError, match=r"Event.objects.values\('neem'\): Event has no field 'neem'"):
        await Event.objects.filter(name="Test").values("name", "neem")


@pytest.mark.asyncio
async def test_values_list_bad_key(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    with pytest.raises(FieldError, match=r"Event.objects.values_list\('neem'\): Event has no field 'neem'"):
        await Event.objects.filter(name="Test").values_list("name", "neem")


@pytest.mark.asyncio
async def test_values_related_bad_key(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    with pytest.raises(FieldError, match='Unknown field "tournament__neem": Tournament has no field "neem"'):
        await Event.objects.filter(name="Test").values("name", "tournament__neem")


@pytest.mark.asyncio
async def test_values_list_related_bad_key(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)

    with pytest.raises(FieldError, match='Unknown field "tournament__neem": Tournament has no field "neem"'):
        await Event.objects.filter(name="Test").values_list("name", "tournament__neem")


@pytest.mark.asyncio
async def test_values_list_annotations_length(db):
    await Tournament.objects.create(name="Championship")
    await Tournament.objects.create(name="Super Bowl")

    tournaments = await Tournament.objects.annotate(name_length=Length("name")).values_list("name", "name_length")
    assert sorted(tournaments) == sorted([("Championship", 12), ("Super Bowl", 10)])


@pytest.mark.asyncio
async def test_values_annotations_length(db):
    await Tournament.objects.create(name="Championship")
    await Tournament.objects.create(name="Super Bowl")

    tournaments = await Tournament.objects.annotate(name_slength=Length("name")).values("name", "name_slength")
    assert sorted(tournaments, key=lambda x: x["name"]) == sorted(
        [
            {"name": "Championship", "name_slength": 12},
            {"name": "Super Bowl", "name_slength": 10},
        ],
        key=lambda x: x["name"],
    )


@pytest.mark.asyncio
async def test_values_list_annotations_trim(db):
    await Tournament.objects.create(name="  x")
    await Tournament.objects.create(name=" y ")

    tournaments = await Tournament.objects.annotate(name_trim=Trim("name")).values_list("name", "name_trim")
    assert sorted(tournaments) == sorted([("  x", "x"), (" y ", "y")])


@pytest.mark.asyncio
async def test_values_annotations_trim(db):
    await Tournament.objects.create(name="  x")
    await Tournament.objects.create(name=" y ")

    tournaments = await Tournament.objects.annotate(name_trim=Trim("name")).values("name", "name_trim")
    assert sorted(tournaments, key=lambda x: x["name"]) == sorted(
        [{"name": "  x", "name_trim": "x"}, {"name": " y ", "name_trim": "y"}],
        key=lambda x: x["name"],
    )


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


@pytest.mark.asyncio
async def test_order_by_annotation_not_in_values(db):
    await Tournament.objects.create(name="2")
    await Tournament.objects.create(name="3")
    await Tournament.objects.create(name="1")

    tournaments = (
        await Tournament.objects.annotate(
            name_orderable=Case(
                When(Q(name="1"), then="a"),
                When(Q(name="2"), then="b"),
                When(Q(name="3"), then="c"),
                default="z",
            )
        )
        .order_by("name_orderable")
        .values("name")
    )
    assert [t["name"] for t in tournaments] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_order_by_annotation_not_in_values_list(db):
    await Tournament.objects.create(name="2")
    await Tournament.objects.create(name="3")
    await Tournament.objects.create(name="1")

    tournaments = (
        await Tournament.objects.annotate(
            name_orderable=Case(
                When(Q(name="1"), then="a"),
                When(Q(name="2"), then="b"),
                When(Q(name="3"), then="c"),
                default="z",
            )
        )
        .order_by("name_orderable")
        .values_list("name")
    )
    assert tournaments == [("1",), ("2",), ("3",)]
