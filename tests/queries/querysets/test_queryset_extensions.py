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
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import ArrayJoin, AsofJoin, OuterReference, Q
from hare.query.plans.recording.plan_recording import PlanRecording
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.extensions import QuerySetExtensions
from hare.query.queryset.extensions.enums import ExtensionPhase
from hare.sql.builder.queries import QueryBuilder
from hare.sql.terms.field import Field
from tests.shared_models.testmodels import Event, TenantScopedFactory, TenantScopedWidget, Tournament
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.statement_plan_checks import StatementPlanChecks


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
    rows, hits = await StatementPlanChecks.count_plan_hits(
        Tournament.objects.filter(name__gte="b").order_by("name").first_rows("c")
    )
    assert [tournament.name for tournament in rows] == ["b"]
    await queryset.count()
    count, hits = await StatementPlanChecks.count_plan_hits(queryset.count())
    assert (count, hits) == (2, 1)
    await Tournament.objects.all().first_rows("c").values_list("name", flat=True)
    names, hits = await StatementPlanChecks.count_plan_hits(
        Tournament.objects.all().first_rows("b").values_list("name", flat=True)
    )
    assert (names, hits) == (["a"], 0)
    names, hits = await StatementPlanChecks.count_plan_hits(
        Tournament.objects.all().first_rows("b").values_list("name", flat=True)
    )
    assert (names, hits) == (["a"], 1)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_an_unhashable_argument_keeps_no_plan(db, first_rows_method):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)
    for names in (["a"], ["b", "c"], ["a"]):
        rows, hits = await StatementPlanChecks.count_plan_hits(Tournament.objects.all().order_by("name").named(names))
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
    for dialect_class in {
        SqliteDialect,
        PostgresqlDialect,
        type(HareContext.require_current().apps.get_queryset(Tournament).get_connection().dialect),
    }:
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
        names_read, hits = await StatementPlanChecks.count_plan_hits(
            Tournament.objects.all().matching(Q(name__gte=name_from)).order_by("name").values_list("name", flat=True)
        )
        assert (names_read, hits) == (names, 1)
    assert await Tournament.objects.matching(Q(name="a") | Q(name="c"), id__gt=0).count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", (ExtensionPhase.ROW_SOURCE, ExtensionPhase.STATEMENT))
@pytest.mark.parametrize("model_scopes", (False, True), ids=("shared-tenant", "model-tenants"))
async def test_extension_joins_bind_the_current_models_tenant_scopes(query_reading_methods, phase, model_scopes):
    dialect_class = type(HareContext.require_current().apps.get_queryset(TenantScopedWidget).get_connection().dialect)
    QuerySetExtensions.register(
        "scoped_matching", dialect_class, matching, reads_query=True, takes_condition=True, phase=phase
    )
    for tenant in (1, 2):
        factory_tenant = tenant + 100 if model_scopes else tenant
        scope = {TenantScopedWidget: tenant, TenantScopedFactory: factory_tenant} if model_scopes else tenant
        with Tenancy.scope(scope):
            factory = await TenantScopedFactory.objects.create(id=tenant, name=f"factory-{tenant}")
            await TenantScopedWidget.objects.create(id=tenant, name=f"widget-{tenant}", factory=factory)
    for tenant in (1, 2, 1):
        factory_tenant = tenant + 100 if model_scopes else tenant
        scope = {TenantScopedWidget: tenant, TenantScopedFactory: factory_tenant} if model_scopes else tenant
        with Tenancy.scope(scope):
            queryset = TenantScopedWidget.objects.scoped_matching(factory__name=f"factory-{tenant}").order_by("id")
            assert [obj.name for obj in await queryset] == [f"widget-{tenant}"]
            assert await queryset.values_list("name", flat=True) == [f"widget-{tenant}"]
            assert await queryset.count() == 1
            assert await TenantScopedWidget.objects.filter(factory__name=f"factory-{tenant}").values_list(
                "name", flat=True
            ) == [f"widget-{tenant}"]


@pytest.mark.asyncio
async def test_failed_extension_build_restores_the_enclosing_plan_recording(query_reading_methods):
    def reject_condition(builder, extension_query, condition):
        matching(builder, extension_query, condition)
        raise ValueError("rejected extension condition")

    QuerySetExtensions.register(
        "rejected_matching",
        type(HareContext.require_current().apps.get_queryset(Tournament).get_connection().dialect),
        reject_condition,
        reads_query=True,
        takes_condition=True,
    )
    enclosing = PlanRecording()
    token = PlanRecording.current.set(enclosing)
    try:
        with pytest.raises(ValueError, match="rejected extension condition"):
            await Tournament.objects.rejected_matching(name="a")
        assert PlanRecording.current.get() is enclosing
    finally:
        PlanRecording.current.reset(token)


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
@pytest.mark.parametrize("phase", [ExtensionPhase.ROW_SOURCE, ExtensionPhase.STATEMENT])
@pytest.mark.parametrize("shape", ["models", "flat", "dict", "distinct", "combined-models", "combined-flat"])
async def test_iterator_pages_the_complete_extension_source(db, monkeypatch, phase, shape):
    monkeypatch.setattr(Registries, "snapshot", dataclasses.replace(Registries.snapshot, methods=MappingProxyType({})))

    def bounded_rows(builder, count):
        return builder.limit(count)

    QuerySetExtensions.register(
        "bounded_rows", type(DatabaseUnderTest.get_dialect()), bounded_rows, changes_rows=True, phase=phase
    )
    for name in ("b", "a", "b", "c"):
        await Tournament.objects.create(name=name)
    source = Tournament.objects.order_by("name")
    if shape == "flat":
        source = source.values_list("name", flat=True)
    elif shape == "dict":
        source = source.values("name")
    elif shape == "distinct":
        source = source.only("name").distinct()
    elif shape == "combined-models":
        source = source.union(source.none()).order_by("name")
    elif shape == "combined-flat":
        source = source.values_list("name", flat=True)
        source = source.union(source.none(), all=True).order_by("name")
    names = ["a", "b", "c"] if shape == "distinct" else ["a", "b", "b", "c"]
    for caller_limit in (4, 5):
        for count in (2, 0, 3, 2):
            query = source.limit(caller_limit).bounded_rows(count)
            expected = names[:count]
            rows = await query
            assert [
                row if shape in ("flat", "combined-flat") else row["name"] if shape == "dict" else row.name
                for row in rows
            ] == expected
            for chunk_size in (1, 4):
                rows = [row async for row in query.iterator(chunk_size=chunk_size)]
                assert [
                    row if shape in ("flat", "combined-flat") else row["name"] if shape == "dict" else row.name
                    for row in rows
                ] == expected


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
    if HareContext.require_current().apps.get_queryset(Event).get_connection().features.supports_asof_join:
        pytest.skip("the database joins ASOF")
    with pytest.raises(UnSupportedError, match="needs a join of the closest row"):
        await queryset


@pytest.mark.asyncio
async def test_an_array_join_needs_its_database(db):
    queryset = Tournament.objects.alias(letter=ArrayJoin("name")).values("letter")
    if HareContext.require_current().apps.get_queryset(Tournament).get_connection().features.supports_array_join:
        pytest.skip("the database joins an array's elements")
    with pytest.raises(UnSupportedError, match="needs a join of an array.s elements"):
        await queryset
