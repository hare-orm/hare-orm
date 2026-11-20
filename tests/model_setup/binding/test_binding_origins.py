from __future__ import annotations

import gc
import pickle
import weakref
from copy import copy, deepcopy
from types import ModuleType

import pytest

from hare import Hare, Model, fields
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.apps.model_binding_preparation import ModelBindingPreparation
from hare.core.caching.cache import Cache
from hare.core.caching.caches import Caches
from hare.core.hare_context import HareContext
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.view import View
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.exceptions import ConfigurationError, QueryError
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.native.native_modules import NativeModules
from hare.query.expressions import Exists, Subquery
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.rows.model_rows.model_columns import ModelColumns
from tests.model_setup.binding import construction_models
from tests.model_setup.binding.construction_models import ConstructionAuthor, ConstructionBook
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent


@pytest.fixture
def marker_cache():
    cache = Cache()
    try:
        yield cache
    finally:
        cache.clear()
        Caches.registered.remove(cache)


CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"library": {"models": [construction_models]}},
}


def test_offline_registry_construction_keeps_one_graph_through_custom_constructors():
    apps = Hare.bind_models(CONFIG)
    obj = apps.new(ConstructionBook, title="created")
    binding = apps.get_binding(ConstructionBook)
    assert obj._binding is binding and obj._context is None
    assert obj.new_author._binding is apps.get_binding(ConstructionAuthor)
    assert obj.initial_author._binding is apps.get_binding(ConstructionAuthor)
    assert InstanceInitialization.construction.get() is None


def test_graph_fact_invalidation_does_not_touch_another_registry():
    first = Hare.bind_models(CONFIG)
    second = Hare.bind_models(CONFIG)
    cache = Cache(depends_on_other_models=True)
    first_author, first_book = first.get_binding(ConstructionAuthor), first.get_binding(ConstructionBook)
    second_author = second.get_binding(ConstructionAuthor)
    try:
        cache.get_bucket(first_author)["value"] = "first author"
        cache.get_bucket(first_book)["value"] = "first book"
        cache.get_bucket(second_author)["value"] = "second author"
        cache.forget_model(first_author)
        assert not cache.get_bucket(first_author)
        assert not cache.get_bucket(first_book)
        assert cache.get_bucket(second_author)["value"] == "second author"
    finally:
        cache.clear()
        Caches.registered.remove(cache)


@pytest.mark.asyncio
async def test_template_replay_and_retained_query_sql_use_independent_contexts():
    with HareContext():
        template = ConstructionBook.objects.filter(title="retained").values_list("title", flat=True)
    async with HareContext() as first:
        await first.init(CONFIG, connect=False, table_name_generator=lambda model: "first_" + model.__name__.lower())
        query = CallsBeforeSetup.bind(template, first)
        before = query.sql()
        async with HareContext() as second:
            await second.init(
                CONFIG, connect=False, table_name_generator=lambda model: "second_" + model.__name__.lower()
            )
            other = CallsBeforeSetup.bind(template, second)
            assert query.sql() == before
            assert other.sql() != before
            assert template._binding is None and template._calls_before_setup is not None
            assert query._binding is first.apps.get_binding(ConstructionBook)
            assert other._binding is second.apps.get_binding(ConstructionBook)
            assert HareContext.require_current() is second
            raw = query.raw('SELECT 1 AS "id"')
            assert raw._binding is query._binding and raw._context is first
        assert query.sql() == before
    with pytest.raises(ConfigurationError, match="closed"):
        query.sql()


@pytest.mark.asyncio
async def test_custom_and_native_allocation_keep_the_supplied_graph_under_another_context():
    async with HareContext() as first:
        await first.init(CONFIG, connect=False)
        binding = first.apps.get_binding(ConstructionBook)
        async with HareContext() as second:
            await second.init(CONFIG, connect=False)
            obj = first.apps.new(ConstructionBook, title="created")
            assert obj._binding is binding and obj._context is first
            assert obj.new_author._binding.graph is binding.graph
            assert obj.initial_author._binding.graph is binding.graph
            columns = ModelColumns.from_entries(binding, (("id", None, None), ("title", None, None)), (0, 1), False)
            row = NativeModules.rows.ModelResultLayout((columns,), ()).read_row(
                (7, "loaded"), "default", first, lambda instance: None
            )
            assert row._binding is binding and row._context is first
            assert row.new_author._binding is first.apps.get_binding(ConstructionAuthor)
            assert row.new_author._context is first
            assert HareContext.require_current() is second
            assert InstanceInitialization.construction.get() is None


@pytest.mark.asyncio
async def test_mixed_graph_queries_are_rejected_even_with_equal_connection_aliases():
    async with HareContext() as first:
        await first.init(CONFIG, connect=False)
        left = ConstructionBook.objects.values_list("id", flat=True)
        async with HareContext() as second:
            await second.init(CONFIG, connect=False)
            right = ConstructionBook.objects.values_list("id", flat=True)
            with pytest.raises(QueryError, match="different model graphs or contexts"):
                left.union(right)
            for expression in (Subquery(right), Exists(right)):
                with pytest.raises(QueryError, match="different model graphs or contexts"):
                    left.annotate(nested=expression).values_list("nested", flat=True).sql()
            with pytest.raises(QueryError, match="different model graphs or contexts"):
                left.with_cte("foreign_rows", right)


@pytest.mark.asyncio
async def test_pickle_restores_loaded_relation_cycles_detached_until_explicit_attachment(marker_cache):
    async with HareContext() as context:
        await context.init(CONFIG, connect=False)
        author = context.apps.construct(ConstructionAuthor, id=1, name="Author", _saved_in_db=True)
        book = context.apps.construct(ConstructionBook, id=2, title="Book", _saved_in_db=True)
        book.author = author
        rows = RelationRows()
        rows.set_fetched([book])
        author._books = rows
        for duplicate in (copy(book), deepcopy(book)):
            assert duplicate._binding is book._binding and duplicate._context is context
        marker_cache.get_bucket(book._binding)["sentinel"] = context
        restored = pickle.loads(pickle.dumps(book))
        assert restored._context is None and restored.author._context is None
        assert restored.author._books.related_objects[0] is restored
        assert "sentinel" not in marker_cache.get_bucket(restored._binding)
        with pytest.raises(ConfigurationError, match="detached"):
            await restored.save()
        graph = restored._binding.graph
        context.attach(restored)
        assert restored._context is context and restored.author._context is context
        assert restored._binding.graph is graph
        assert restored.author._books.related_objects[0] is restored


def test_sealing_failure_preserves_the_published_registry_and_graph(monkeypatch):
    apps = Hare.bind_models(CONFIG)
    previous_models = apps.apps
    previous_graph = apps.binding_graph
    preparation = ModelBindingPreparation(apps)
    seal = ModelBindingGraph.seal

    def fail_candidate(graph):
        if graph is preparation.graph:
            raise RuntimeError("seal failed")
        seal(graph)

    monkeypatch.setattr(ModelBindingGraph, "seal", fail_candidate)
    with pytest.raises(RuntimeError, match="seal failed"):
        with preparation:
            apps.apps["candidate"] = {}
    assert apps.apps is previous_models
    assert apps.binding_graph is previous_graph
    assert apps._preparation is None
    assert "candidate" not in apps.apps


@pytest.mark.asyncio
async def test_offline_nested_templates_do_not_capture_an_active_application():
    with HareContext():
        template = ConstructionBook.objects.filter(title="nested")
    apps = Hare.bind_models(CONFIG)
    query = apps.get_queryset(ConstructionBook).annotate(nested=Exists(template)).values_list("nested", flat=True)
    async with HareContext() as active:
        await active.init(CONFIG, connect=False, table_name_generator=lambda model: "active_" + model.__name__.lower())
        sql = query.using(active.connections.get("default")).sql()
        assert apps.get_binding(ConstructionBook).db_table in sql
        assert active.apps.get_binding(ConstructionBook).db_table not in sql
        assert query._context is None and template._binding is None
        assert HareContext.require_current() is active


@pytest.mark.asyncio
async def test_a_view_captures_an_explicit_source_without_retaining_its_application():
    async with HareContext() as source:
        await source.init(CONFIG, connect=False)
        query = source.apps.get_queryset(ConstructionBook).values("id", "title")
        expected = query.sql(parameters_inline=True)
        view = View("book_titles", query=query)
        assert isinstance(view.query, RawSQLTerm)
        assert view._binding is None
    apps = Hare.bind_models(CONFIG)
    prepared = view.for_binding(apps.get_binding(ConstructionBook))
    assert prepared.get_query_sql() == expected


@pytest.mark.asyncio
async def test_a_retained_native_layout_does_not_retain_application_resources():
    async def prepare_layout():
        async with HareContext() as context:
            await context.init(CONFIG, connect=False)
            binding = context.apps.get_binding(ConstructionBook)
            columns = ModelColumns.from_entries(binding, (("id", None, None), ("title", None, None)), (0, 1), False)
            layout = NativeModules.rows.ModelResultLayout((columns,), ())
            return weakref.ref(context), layout

    context_reference, layout = await prepare_layout()
    gc.collect()
    assert context_reference() is None
    assert layout is not None


@pytest.mark.requires("request-query")
def test_parameter_schemas_can_be_prepared_for_explicit_offline_registries():
    from hare.contrib.request_query import FilterField, RequestQuery

    with HareContext():

        class BookQuery(RequestQuery[ConstructionBook]):
            class Meta:
                queryset = ConstructionBook.objects.all()
                filters = (FilterField("title"),)

    first = Hare.bind_models(CONFIG)
    second = Hare.bind_models(CONFIG)
    first_schema = BookQuery.prepare_parameters(apps=first)
    second_schema = BookQuery.prepare_parameters(apps=second)
    assert first_schema is BookQuery.prepare_parameters(apps=first)
    assert first_schema is not second_schema
    assert first_schema.model_fields["title"].annotation == str | None
    first_query = first_schema.get_declaration().queryset
    second_query = second_schema.get_declaration().queryset
    assert first_query._binding is first.get_binding(ConstructionBook)
    assert second_query._binding is second.get_binding(ConstructionBook)
    assert first_query._context is None and second_query._context is None
    assert BookQuery.Meta.queryset._binding is None


def test_historical_registration_publishes_partial_and_complete_graphs_independently():
    class HistoricalAuthor(Model):
        name = fields.TextField()

    class HistoricalBook(Model):
        author = fields.ForeignKeyField("history.HistoricalAuthor", related_name="books")

    apps = StateApps(environment=MigrationEnvironment())
    apps.register_model("history", HistoricalBook)
    partial = apps.get_binding(HistoricalBook)
    assert not partial._inited
    apps.register_model("history", HistoricalAuthor)
    complete = apps.get_binding(HistoricalBook)
    assert complete._inited and complete is not partial
    assert not partial._inited
    assert complete.fields_map["author"].get_related_binding() is apps.get_binding(HistoricalAuthor)
    captured = MigrationEnvironment.from_apps(apps)
    assert captured.source_bindings[HistoricalBook] is complete
    assert HistoricalBook not in apps.environment.source_bindings


def test_a_registry_does_not_replace_an_explicit_class_reference_by_its_label():
    original = type("SharedName", (Model,), {"Meta": type("Meta", (), {"app": "library"})})
    replacement = type("SharedName", (Model,), {"Meta": type("Meta", (), {"app": "library"})})
    module = ModuleType("replacement_model_declaration")
    module.__models__ = [replacement]
    apps = Hare.bind_models(
        {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"library": {"models": [module]}},
        }
    )
    assert apps.get_model("library.SharedName") is replacement
    assert apps.get_model(replacement) is replacement
    with pytest.raises(ConfigurationError, match="not registered"):
        apps.get_model(original)
    with pytest.raises(ConfigurationError, match="not registered"):
        apps.get_binding(original)


def test_a_frozen_projection_keeps_its_source_model_check():
    apps = Hare.bind_models(CONFIG)
    source = apps.get_queryset(ConstructionBook).values("title").order_by("title").using(FakeClient("clickhouse"))
    projection = ClickhouseProjection("book_titles", source)
    assert isinstance(projection.query, RawSQLTerm)
    assert projection.for_binding(apps.get_binding(ConstructionBook)) is projection
    with pytest.raises(ConfigurationError, match="a projection is a query of its table"):
        projection.for_binding(apps.get_binding(ConstructionAuthor))
    with pytest.raises(ConfigurationError, match="a projection is a query of its table"):
        projection.with_sql_query(apps.get_binding(ConstructionAuthor))


@pytest.mark.asyncio
async def test_state_capture_keeps_effective_table_names_and_connection_placement():
    async with HareContext() as context:
        await context.init(
            CONFIG, connect=False, table_name_generator=lambda model: "captured_" + model.__name__.lower()
        )
        state = State.from_models((ConstructionAuthor, ConstructionBook))
        assert state.default_connections == {"library": "default"}
        before = context.apps.get_binding(ConstructionBook)
        after = state.apps.get_binding("library.ConstructionBook")
        assert after.db_table == before.db_table
        assert after.default_connection == before.default_connection
        assert after.fields_map["author"].get_related_binding() is state.apps.get_binding("library.ConstructionAuthor")
