"""A query that hits the query-shape cache runs the shape's compiled SQL text with its own
parameters - no copy of the stored query, no clone of its WHERE tree, no render."""

import datetime
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from hare.query.expressions import F, RawSQL
from hare.query.functions import Count, Max, Upper
from hare.query.queryset import QuerySet
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.sql.queries.builder import QueryBuilder
from tests.testmodels import Event, Tournament


async def create_tournaments() -> list[Tournament]:
    return [
        await Tournament.objects.create(name=name, desc=f"{name} desc") for name in ("alpha", "beta", "gamma", "delta")
    ]


@contextmanager
def full_build():
    """Runs a query the way it ran before compiled statements: always built and rendered."""
    with (
        patch.object(AwaitableQuery, "_run_on_plan", lambda self, *args, **kwargs: False),
        patch.object(QuerySet, "_await_call_signature_rows", lambda self, *args, **kwargs: None),
    ):
        yield


@pytest.mark.asyncio
async def test_a_second_query_of_a_shape_renders_no_sql(db):
    tournaments = await create_tournaments()
    assert (await Tournament.objects.filter(name="alpha").first()).pk == tournaments[0].pk
    assert (await Tournament.objects.filter(name="beta").first()).pk == tournaments[1].pk
    real_get_parameterized_sql = QueryBuilder.get_parameterized_sql
    rendered = []

    def counting_get_parameterized_sql(self, *args, **kwargs):
        rendered.append(self)
        return real_get_parameterized_sql(self, *args, **kwargs)

    with patch.object(QueryBuilder, "get_parameterized_sql", counting_get_parameterized_sql):
        assert (await Tournament.objects.filter(name="gamma").first()).pk == tournaments[2].pk
        assert await Tournament.objects.filter(name="nothing").first() is None
    assert rendered == []
    # A full build renders - the check above would notice a render.
    with patch.object(QueryBuilder, "get_parameterized_sql", counting_get_parameterized_sql), full_build():
        await Tournament.objects.filter(name="delta").first()
    assert len(rendered) == 1


SHAPES = [
    ("equality", lambda value: Tournament.objects.filter(name=value), ["alpha", "beta"]),
    (
        "in list",
        lambda value: Tournament.objects.filter(name__in=value).order_by("id"),
        [["alpha", "beta"], ["gamma", "delta"]],
    ),
    ("startswith", lambda value: Tournament.objects.filter(name__startswith=value), ["al", "ga"]),
    ("range", lambda value: Tournament.objects.filter(id__range=value).order_by("id"), "id ranges"),
    ("limit offset", lambda value: Tournament.objects.all().order_by("id").limit(value).offset(value - 1), [1, 2]),
    (
        "annotate",
        lambda value: Tournament.objects.annotate(upper=Upper("name")).filter(upper=value),
        ["ALPHA", "DELTA"],
    ),
    ("count", lambda value: Tournament.objects.filter(id__gte=value).count(), "ids"),
    ("exists", lambda value: Tournament.objects.filter(name=value).exists(), ["beta", "missing"]),
    (
        "values_list",
        lambda value: Tournament.objects.filter(id__lte=value).order_by("id").values_list("name", flat=True),
        "ids",
    ),
    ("values", lambda value: Tournament.objects.filter(name=value).values("id", "name"), ["gamma", "alpha"]),
    ("relation", lambda value: Event.objects.filter(tournament_id=value).order_by("name"), "ids"),
]


def comparable(result):
    if isinstance(result, list):
        return [comparable(item) for item in result]
    if isinstance(result, Tournament | Event):
        return ("model", type(result).__name__, result.pk)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values"), SHAPES, ids=[shape[0] for shape in SHAPES])
async def test_a_compiled_query_returns_what_the_full_build_returns(db, name, build, values):
    tournaments = await create_tournaments()
    for tournament in tournaments:
        await Event.objects.create(name=f"{tournament.name} event", tournament=tournament)
    ids = [tournament.pk for tournament in tournaments]
    if values == "ids":
        values = [ids[0], ids[2]]
    elif values == "id ranges":
        values = [(ids[0], ids[1]), (ids[1], ids[3])]
    first_value, second_value = values
    await build(first_value)
    compiled = [comparable(await build(value)) for value in (second_value, first_value)]
    with full_build():
        built = [comparable(await build(value)) for value in (second_value, first_value)]
    assert compiled == built
    assert compiled[0] != compiled[1] or first_value == second_value or name == "exists"


RENDERLESS_SHAPES = [
    (
        "filter on an aggregate annotation",
        lambda value: (
            Tournament.objects.annotate(event_count=Count("events")).filter(event_count__gte=value).order_by("id")
        ),
        [1, 2],
    ),
    (
        "filter on an annotation holding a literal",
        lambda value: Tournament.objects.annotate(doubled=F("id") * 2).filter(doubled__gt=value).order_by("id"),
        "ids",
    ),
    (
        "aggregate over a sliced values query",
        lambda value: (
            Tournament.objects.filter(id__gte=value).order_by("id").values("id")[1:3].aggregate(top=Max("id"))
        ),
        "ids",
    ),
    (
        "aggregate with a filter",
        lambda value: Tournament.objects.filter(id__gte=value).aggregate(top=Max("id"), total=Count("id")),
        "ids",
    ),
    (
        "exists of a query annotated with a literal",
        lambda value: Tournament.objects.annotate(later=F("id") + value).filter(id__gte=1).exists(),
        [1, 2],
    ),
    (
        "count of a query annotated with a literal",
        lambda value: Tournament.objects.annotate(later=F("id") + value).filter(id__gte=1).count(),
        [1, 2],
    ),
    (
        "count of a sliced values query",
        lambda value: (
            Tournament.objects.filter(id__gte=value).order_by("id").values_list("name", flat=True)[1:3].count()
        ),
        "ids",
    ),
    (
        "exists past an offset",
        lambda value: (
            Tournament.objects.filter(id__gte=value).order_by("id").values_list("id", flat=True)[2:].exists()
        ),
        "ids",
    ),
    (
        "count of distinct values",
        lambda value: Tournament.objects.filter(id__gte=value).values_list("desc", flat=True).distinct().count(),
        "ids",
    ),
    (
        "count of a values union",
        lambda value: (
            Tournament.objects.filter(id=value)
            .values_list("name", flat=True)
            .union(Tournament.objects.filter(name="beta").values_list("name", flat=True))
            .count()
        ),
        "ids",
    ),
    ("keyset page", lambda value: Tournament.objects.all().order_by("id").after_cursor(value), "ids"),
    (
        "keyset page of two fields",
        lambda value: Tournament.objects.all().order_by("name", "id").after_cursor(value, 0),
        ["alpha", "delta"],
    ),
    (
        "keyset page after a null",
        lambda value: Tournament.objects.all().order_by("desc", "id").after_cursor(None, value),
        "ids",
    ),
    (
        "values_list of an annotation",
        lambda value: (
            Tournament.objects.annotate(upper=Upper("name"))
            .filter(id__gte=value)
            .order_by("id")
            .values_list("upper", flat=True)
        ),
        "ids",
    ),
    (
        "date arithmetic",
        lambda value: (
            Event.objects.annotate(later=F("modified") + value).order_by("event_id").values_list("later", flat=True)
        ),
        [datetime.timedelta(hours=1), datetime.timedelta(days=2)],
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values"), RENDERLESS_SHAPES, ids=[shape[0] for shape in RENDERLESS_SHAPES])
async def test_a_later_query_of_the_shape_renders_no_sql(db, name, build, values):
    tournaments = await create_tournaments()
    for tournament in tournaments:
        await Event.objects.create(name=f"{tournament.name} event", tournament=tournament)
    if values == "ids":
        values = [tournaments[0].pk, tournaments[2].pk]
    first_value, second_value = values
    await build(first_value)
    real_get_parameterized_sql = QueryBuilder.get_parameterized_sql
    rendered = []

    def counting_get_parameterized_sql(self, *args, **kwargs):
        rendered.append(self)
        return real_get_parameterized_sql(self, *args, **kwargs)

    with patch.object(QueryBuilder, "get_parameterized_sql", counting_get_parameterized_sql):
        compiled = comparable(await build(second_value))
    assert rendered == []
    with full_build():
        assert compiled == comparable(await build(second_value))


@pytest.mark.asyncio
async def test_sql_of_a_cached_shape_shows_its_own_values(db):
    """.sql() isn't a run - it renders the query with its own values, not the compiled text's."""
    await create_tournaments()
    await Tournament.objects.filter(name="alpha")
    await Tournament.objects.filter(name="beta")
    assert "'gamma'" in Tournament.objects.filter(name="gamma").sql(params_inline=True)


@pytest.mark.asyncio
async def test_a_subquery_of_a_compiled_shape_keeps_its_own_values(db):
    """A query built into another one - here as an ``__in`` subquery - renders into the outer
    statement with its own values, never the compiled text's stored ones."""
    tournaments = await create_tournaments()
    await Tournament.objects.filter(name="alpha").values_list("id", flat=True)
    await Tournament.objects.filter(name="beta").values_list("id", flat=True)
    inner = Tournament.objects.filter(name="gamma").values_list("id", flat=True)
    found = await Tournament.objects.filter(id__in=inner)
    assert [tournament.pk for tournament in found] == [tournaments[2].pk]


@pytest.mark.asyncio
async def test_a_parameter_of_another_type_takes_the_full_build(db):
    """A parameter's cast can follow its type (a RawSQL parameter on PostgreSQL is cast to the
    type of its value) - a value of another type than the compiled text's never reuses it."""
    await create_tournaments()
    for value in (5, 6, 5.5, 2):
        rows = await Tournament.objects.all().annotate(x=RawSQL("%s", [value])).order_by("id").values("id", "x")
        assert rows[0]["x"] == value
        assert type(rows[0]["x"]) is type(value)
