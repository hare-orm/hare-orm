"""QuerySet methods a dialect registers (QuerySetExtensions) - recorded on the queryset, applied
through the implementation of the connection's dialect, refused on another dialect."""

import sys
import types

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.models import Model
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.extensions import QuerySetExtensions
from hare.sql.queries.builder import QueryBuilder
from hare.sql.terms.field import Field
from tests.testmodels import Tournament


def first_rows(builder: QueryBuilder, name_below: str) -> QueryBuilder:
    return builder.where(Field("name") < name_below)


def first_values(builder: QueryBuilder, value_below: int) -> QueryBuilder:
    return builder.where(Field("value") < value_below)


def named(builder: QueryBuilder, names: list[str]) -> QueryBuilder:
    return builder.where(Field("name").isin(names))


@pytest.fixture
def first_rows_method(monkeypatch):
    monkeypatch.setattr(QuerySetExtensions, "registered", {})
    QuerySetExtensions.register("first_rows", "sqlite", first_rows)
    QuerySetExtensions.register("first_values", "sqlite", first_values)
    QuerySetExtensions.register("named", "sqlite", named)


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


class DeferredExtensionReading(Model):
    value = fields.IntField()


def test_a_method_can_not_shadow_a_queryset_attribute(first_rows_method):
    with pytest.raises(ConfigurationError, match="can't be named 'filter'"):
        QuerySetExtensions.register("filter", "sqlite", first_rows)
    with pytest.raises(ConfigurationError, match="can't be named '_private'"):
        QuerySetExtensions.register("_private", "sqlite", first_rows)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_the_method_is_applied_by_the_connection_dialect(db, first_rows_method):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    queryset = Tournament.objects.all().order_by("name").first_rows("c")
    assert await queryset.values_list("name", flat=True) == ["a", "b"]
    assert await queryset.count() == 2
    assert [tournament.name for tournament in await queryset] == ["a", "b"]
    with pytest.raises(AttributeError):
        Tournament.objects.all().no_such_method()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_each_call_keeps_its_own_plan(db, first_rows_method):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    StatementPlans.plans.clear()
    for _ in range(2):
        for name_below, names in (("b", ["a"]), ("c", ["a", "b"]), ("d", ["a", "b", "c"])):
            queryset = Tournament.objects.filter(name__gte="a").order_by("name").first_rows(name_below)
            assert [tournament.name for tournament in await queryset] == names
            assert await queryset.values_list("name", flat=True) == names
            assert await queryset.count() == len(names)
            assert await queryset.exists()
        assert [tournament.name for tournament in await Tournament.objects.filter(name__gte="a").order_by("name")] == [
            "a",
            "b",
            "c",
        ]


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_query_with_calls_runs_on_its_plan(db, first_rows_method):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    queryset = Tournament.objects.all().order_by("name").first_rows("c")
    await queryset
    rows, hits = await count_plan_hits(Tournament.objects.filter(name__gte="b").order_by("name").first_rows("c"))
    assert [tournament.name for tournament in rows] == ["b"]
    await queryset.count()
    count, hits = await count_plan_hits(queryset.count())
    assert (count, hits) == (2, 1)
    await Tournament.objects.all().first_rows("c").values_list("name", flat=True)
    names, hits = await count_plan_hits(Tournament.objects.all().first_rows("b").values_list("name", flat=True))
    assert (names, hits) == (["a"], 0)
    names, hits = await count_plan_hits(Tournament.objects.all().first_rows("b").values_list("name", flat=True))
    assert (names, hits) == (["a"], 1)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_an_unhashable_argument_keeps_no_plan(db, first_rows_method):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    for names in (["a"], ["b", "c"], ["a"]):
        rows, hits = await count_plan_hits(Tournament.objects.all().order_by("name").named(names))
        assert [tournament.name for tournament in rows] == names
        assert hits == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_plan_hit_still_refuses_a_method_no_longer_registered(db, first_rows_method, monkeypatch):
    await Tournament.objects.create(name="a")
    queryset = Tournament.objects.all().first_rows("c")
    assert len(await queryset) == 1
    monkeypatch.setattr(QuerySetExtensions, "registered", {})
    with pytest.raises(UnSupportedError, match=r"Tournament.first_rows\(\) is a method of the no longer registered"):
        await queryset


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_another_dialect_refuses_the_method(db, first_rows_method):
    with pytest.raises(UnSupportedError, match=r"Tournament.first_rows\(\) is a method of the sqlite dialect"):
        await Tournament.objects.all().first_rows("c")


@pytest.mark.asyncio
async def test_the_call_is_recorded_before_init(first_rows_method):
    queryset = DeferredExtensionReading.objects.all().order_by("value").first_values(2)
    module_name = "tests._queryset_extension_models"
    module = types.ModuleType(module_name)
    module.DeferredExtensionReading = DeferredExtensionReading  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    context = HareContext()
    await context.__aenter__()
    try:
        await context.init(
            config={
                "connections": {"default": "sqlite://:memory:"},
                "apps": {"extension_models": {"models": [module_name], "default_connection": "default"}},
            }
        )
        await context.generate_schemas()
        await DeferredExtensionReading.objects.create(value=2)
        await DeferredExtensionReading.objects.create(value=1)
        assert await queryset.values_list("value", flat=True) == [1]
    finally:
        await context.__aexit__(None, None, None)
        sys.modules.pop(module_name, None)
