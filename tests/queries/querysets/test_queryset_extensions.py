"""QuerySet methods a dialect registers (QuerySetExtensions) - recorded on the queryset, applied
through the implementation of the connection's dialect, refused on another dialect."""

import dataclasses
import sys
import types
from types import MappingProxyType

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.core.hare_context import HareContext
from hare.core.registries import Registries
from hare.dialects.base.dialect import Dialect
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.exceptions import ConfigurationError, QueryError, UnSupportedError
from hare.models import Model
from hare.query.expressions import ArrayJoin, AsofJoin, OuterReference, Q
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.extensions import QuerySetExtensions
from hare.sql.builder.queries import QueryBuilder
from hare.sql.terms.field import Field
from tests.shared_models.testmodels import Event, Tournament


def first_rows(builder: QueryBuilder, name_below: str) -> QueryBuilder:
    return builder.where(Field("name") < name_below)


def first_values(builder: QueryBuilder, value_below: int) -> QueryBuilder:
    return builder.where(Field("value") < value_below)


def named(builder: QueryBuilder, names: list[str]) -> QueryBuilder:
    return builder.where(Field("name").isin(names))


@pytest.fixture
def first_rows_method(monkeypatch):
    monkeypatch.setattr(Registries, "snapshot", dataclasses.replace(Registries.snapshot, methods=MappingProxyType({})))
    QuerySetExtensions.register("first_rows", SqliteDialect, first_rows)
    QuerySetExtensions.register("first_values", SqliteDialect, first_values)
    QuerySetExtensions.register("named", SqliteDialect, named)


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


class DeferredExtensionReading(Model):
    value = fields.IntField()


@pytest.mark.database_independent
def test_a_method_can_not_shadow_a_queryset_attribute(first_rows_method):
    with pytest.raises(ConfigurationError, match="cannot be named 'filter'"):
        QuerySetExtensions.register("filter", SqliteDialect, first_rows)
    with pytest.raises(ConfigurationError, match="cannot be named '_private'"):
        QuerySetExtensions.register("_private", SqliteDialect, first_rows)


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
    monkeypatch.setattr(Registries, "snapshot", dataclasses.replace(Registries.snapshot, methods=MappingProxyType({})))
    with pytest.raises(UnSupportedError, match=r"Tournament.first_rows\(\) is a method of the no longer registered"):
        await queryset


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_another_dialect_refuses_the_method(db, first_rows_method):
    with pytest.raises(UnSupportedError, match=r"Tournament.first_rows\(\) is a method of the sqlite dialect"):
        await Tournament.objects.all().first_rows("c")


@pytest.mark.sqlite_only
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
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
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


def matching(builder: QueryBuilder, extension_query, condition) -> QueryBuilder:
    modifier = extension_query.get_condition(condition)
    builder = extension_query.join(builder, modifier.joins)
    return builder.where(modifier.where_criterion)


def first_by_name(builder: QueryBuilder, extension_query, count: int) -> QueryBuilder:
    return builder.orderby(extension_query.get_expression("name").term).limit(count)


def tagged(result, tag: str):
    return tag, result.result


@pytest.fixture
def query_reading_methods(db, monkeypatch):
    monkeypatch.setattr(Registries, "snapshot", dataclasses.replace(Registries.snapshot, methods=MappingProxyType({})))
    for dialect_class in {SqliteDialect, PostgresqlDialect, type(Tournament._meta.connection.dialect)}:
        QuerySetExtensions.register("matching", dialect_class, matching, reads_query=True, takes_condition=True)
        QuerySetExtensions.register("first_by_name", dialect_class, first_by_name, reads_query=True, changes_rows=True)
        QuerySetExtensions.register("tagged", dialect_class, lambda builder, tag: builder, transform_result=tagged)


def test_a_method_takes_a_condition_alike_on_every_dialect(query_reading_methods):
    with pytest.raises(ConfigurationError, match="same takes_condition"):
        QuerySetExtensions.register("matching", type("OtherDialect", (Dialect,), {"name": "other"}), first_rows)


@pytest.mark.asyncio
async def test_a_condition_resolves_as_a_filter_and_binds_its_values(db, query_reading_methods):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    await Tournament.objects.all().matching(name__gte="b").order_by("name").values_list("name", flat=True)
    for name_from, names in (("b", ["b", "c"]), ("c", ["c"]), ("a", ["a", "b", "c"])):
        names_read, hits = await count_plan_hits(
            Tournament.objects.all().matching(Q(name__gte=name_from)).order_by("name").values_list("name", flat=True)
        )
        assert (names_read, hits) == (names, 1)
    assert await Tournament.objects.matching(Q(name="a") | Q(name="c"), id__gt=0).count() == 2


@pytest.mark.asyncio
async def test_count_and_exists_count_the_rows_a_method_keeps(db, query_reading_methods):
    for name in ("c", "a", "b"):
        await Tournament.objects.create(name=name)
    queryset = Tournament.objects.all().first_by_name(2)
    assert [tournament.name for tournament in await queryset] == ["a", "b"]
    assert await queryset.count() == 2
    assert await queryset.exists()
    assert await Tournament.objects.all().first_by_name(0).exists() is False


@pytest.mark.asyncio
async def test_a_method_shapes_the_executed_result(db, query_reading_methods):
    await Tournament.objects.create(name="a")
    tag, names = await Tournament.objects.all().tagged("names").values_list("name", flat=True)
    assert (tag, names) == ("names", ["a"])


@pytest.mark.asyncio
async def test_result_handlers_do_not_replace_each_other(db, query_reading_methods):
    with pytest.raises(QueryError, match="Only one result-shaping extension"):
        await Tournament.objects.tagged("first").tagged("second").values("name")


@pytest.mark.asyncio
async def test_iterator_yields_rows_without_applying_a_whole_result_handler(db, query_reading_methods):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    queryset = Tournament.objects.tagged("names").order_by("name").values_list("name", flat=True)
    assert [row async for row in queryset.iterator(chunk_size=1)] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_extension_summary_reuses_the_declaration_and_reads_all_metric_columns(db, query_reading_methods):
    for name in ("c", "a", "b"):
        await Tournament.objects.create(name=name)
    from hare.query.functions import Max

    queryset = Tournament.objects.first_by_name(2)
    count = queryset.count()
    for _ in range(2):
        assert await count == 2
        assert await queryset.aggregate(last_name=Max("name")) == {"last_name": "b"}
        assert await queryset.order_by("name").values_list("name", flat=True) == ["a", "b"]


@pytest.mark.database_independent
def test_extension_effects_are_selected_for_the_bound_dialect(monkeypatch):
    from types import SimpleNamespace

    from hare.query.queryset.extensions.query_set_extension import QuerySetExtension
    from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall

    implementations = {
        "pick_rows": {
            SqliteDialect: QuerySetExtension(lambda builder: builder, changes_rows=False),
            PostgresqlDialect: QuerySetExtension(lambda builder: builder, changes_rows=True),
        },
    }
    monkeypatch.setattr(QuerySetExtensions, "get_registered", lambda: implementations)
    for dialect_class, expected in ((SqliteDialect, False), (PostgresqlDialect, True)):
        query = SimpleNamespace(
            model=Tournament,
            dialect=object.__new__(dialect_class),
            _bound_extensions=None,
            _extension_calls=(QuerySetExtensionCall("pick_rows"),),
        )
        assert QuerySetExtensions.changes_rows(query) is expected


@pytest.mark.asyncio
async def test_an_asof_join_needs_its_database(db):
    queryset = Event.objects.alias(
        last=AsofJoin(Tournament, on=Q(name=OuterReference("name"), id__lte=OuterReference("tournament_id")))
    ).values("last__name")
    if Event._meta.connection.features.supports_asof_join:
        pytest.skip("the database joins ASOF")
    with pytest.raises(UnSupportedError, match="needs a join of the closest row"):
        await queryset


@pytest.mark.asyncio
async def test_an_array_join_needs_its_database(db):
    queryset = Tournament.objects.alias(letter=ArrayJoin("name")).values("letter")
    if Tournament._meta.connection.features.supports_array_join:
        pytest.skip("the database joins an array's elements")
    with pytest.raises(UnSupportedError, match="needs a join of an array.s elements"):
        await queryset
