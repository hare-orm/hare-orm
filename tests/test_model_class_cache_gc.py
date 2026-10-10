from __future__ import annotations

import gc
import sys
import types
import weakref
from typing import Any

import pytest

from hare import Hare, fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.caching.cache import Cache
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.fields.field import Field as ModelField
from hare.models import Model
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.lazy_relation_names import LazyRelationNames
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.sql.builder.queries.query_builder import QueryBuilder
from tests.testmodels import Author, Book


@pytest.mark.asyncio
async def test_dynamic_model_classes_are_garbage_collected_after_context_teardown():
    """Model-keyed caches (the deletion graph's backward relations and cascade models, the lazy
    relation names of a query) are weakly keyed: a model class created at runtime and dropped
    afterwards (schema-per-tenant, throwaway test models) must become collectible."""
    module_name = "tests._cache_gc_dynamic_models"
    app_label = "cache_gc_dynamic"

    async def build_and_touch_caches() -> tuple[weakref.ReferenceType, weakref.ReferenceType]:
        class CacheGCTarget(Model):
            id = fields.IntField(primary_key=True)

        class CacheGCOwner(Model):
            id = fields.IntField(primary_key=True)
            target: fields.ForeignKeyRelation[CacheGCTarget] = fields.ForeignKeyField(
                f"{app_label}.CacheGCTarget", related_name="owners"
            )
            tags: fields.ManyToManyRelation[CacheGCTarget] = fields.ManyToManyField(
                f"{app_label}.CacheGCTarget", related_name="tag_owners", db_constraint=False
            )

        module = types.ModuleType(module_name)
        setattr(module, "CacheGCTarget", CacheGCTarget)  # noqa: B010
        setattr(module, "CacheGCOwner", CacheGCOwner)  # noqa: B010
        sys.modules[module_name] = module
        try:
            async with hare_test_context([module_name], app_label=app_label):
                DeletionGraph.get_backward_relations(CacheGCTarget)
                DeletionGraph.get_cascade_models(CacheGCOwner)
                LazyRelationNames.get(CacheGCTarget)
                owner = await CacheGCOwner.objects.create(target=await CacheGCTarget.objects.create())
                await owner.tags.add(await CacheGCTarget.objects.create())
                await CacheGCOwner.objects.filter(target__id__gte=0).prefetch_related("tags").all()
                await owner.delete()
            return weakref.ref(CacheGCTarget), weakref.ref(CacheGCOwner)
        finally:
            sys.modules.pop(module_name, None)

    target_ref, owner_ref = await build_and_touch_caches()
    gc.collect()
    assert target_ref() is None, "CacheGCTarget must be collectible once every ordinary reference is gone"
    assert owner_ref() is None, "CacheGCOwner must be collectible once every ordinary reference is gone"


@pytest.mark.asyncio
async def test_replaced_live_model_class_is_garbage_collected_after_queries_and_writes():
    """A live model unregistered and replaced by a fresh class over the same table must not stay
    reachable through the statement plans, the decode plans, the row readers or the write
    statements of its queries and writes."""

    def build_live_widget() -> type[Model]:
        class LiveWidget(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=20, default="")

            class Meta:
                table = "replaced_live_widget_gc"

        return LiveWidget

    async def register_use_and_get_ref(app_label: str) -> weakref.ReferenceType:
        live_widget = build_live_widget()
        Hare.register_live_models([live_widget], app_label=app_label)
        widget = await live_widget.objects.create(name="a")
        widget.name = "b"
        await widget.save(update_fields=["name"])
        await live_widget.objects.filter(id=widget.id).all()
        await live_widget.objects.get(id=widget.id)
        await live_widget.objects.filter(id=widget.id).values_list("name", flat=True)
        await live_widget.objects.bulk_create([live_widget(name="c")])
        await live_widget.objects.filter(name="c").update(name="d")
        await widget.delete()
        assert any(key[0] is live_widget for key in StatementPlans.plans)
        return weakref.ref(live_widget)

    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}))
        await (
            Hare.get_context()
            .get_connection()
            .execute_script(
                'CREATE TABLE "replaced_live_widget_gc" (id INTEGER PRIMARY KEY, name VARCHAR(20) NOT NULL)'
            )
        )

        old_ref = await register_use_and_get_ref("gc_live")

        Hare.unregister_live_models([Hare.apps["gc_live"]["LiveWidget"]])
        Hare.register_live_models([build_live_widget()], app_label="gc_live")

        gc.collect()
        assert old_ref() is None, (
            "the replaced LiveWidget class must be collectible once no ordinary reference remains"
        )


@pytest.mark.asyncio
async def test_serialize_instances_cache_isolates_replaced_model_into_its_own_bucket():
    """SERIALIZE_INSTANCES_CACHE used to be a plain dict keyed by (model, columns) - each
    compiled `_serialize` closure captures the model's real Field objects by reference (for
    their to_db_value bound methods), which point back at the model via Field.model, so a
    register_live_model() replacement stayed reachable through this cache forever. Now bucketed
    per-model behind a WeakKeyDictionary instead - two distinct classes never collide into the
    same (model, columns) entry even when they reuse the same table/column names."""

    def build_live_widget() -> type[Model]:
        class LiveWidget(Model):
            id = fields.IntField(primary_key=True)
            name = fields.TextField()

            class Meta:
                table = "replaced_live_widget_serialize_gc"

        return LiveWidget

    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}))

        # _compiled_instance_serializer() directly (mirroring this file's own
        # get_backward_relations()/_apply_lazy_relation_defaults()/_make_query() calls above) -
        # serialize_instances() itself would route through the Rust hydrate accelerator instead
        # when it's built (as it is in this dev checkout), never touching this Python-only cache.
        first_widget = build_live_widget()
        Hare.register_live_models([first_widget], app_label="gc_live_serialize")
        # A different class under the same name needs the old one unregistered first.
        Hare.unregister_live_models([first_widget])
        first_serializer = BulkWriteBatches.get_compiled_instance_serializer(
            first_widget, ("name",), SQLITE_DIALECT.types
        )

        second_widget = build_live_widget()
        Hare.register_live_models([second_widget], app_label="gc_live_serialize")
        second_serializer = BulkWriteBatches.get_compiled_instance_serializer(
            second_widget, ("name",), SQLITE_DIALECT.types
        )

        assert first_widget in BulkWriteBatches.SERIALIZE_INSTANCES_CACHE
        assert second_widget in BulkWriteBatches.SERIALIZE_INSTANCES_CACHE
        assert first_serializer is not second_serializer


def test_iter_never_yields_a_key_the_cache_would_not_serve() -> None:
    """Every key `__iter__()`/`keys()` yields must be retrievable through `__getitem__()` right
    after - the standard `Mapping` contract, relied on by `for key in cache: cache[key]` here and
    in tests/test_query_shape_cache.py. An entry written under another query builder than its
    model's current one is a miss for `__getitem__()`, so iteration skips it too; a model whose
    entries were discarded yields nothing."""
    cache: Cache[tuple[Any, ...]] = Cache(max_size=8)
    try:
        cache[(Author, "shape")] = ("author value",)
        cache[(Book, "shape")] = ("book value",)
        # As if Author's entry had been rendered while another context's builder was bound.
        Author._meta.plan_cache_buckets[id(cache)][(0, ("shape",))] = (object(), ("author value",))

        for key in cache:
            cache[key]  # must never raise KeyError for a key __iter__ itself just yielded
        assert list(cache) == [(Book, "shape")]

        cache.forget_model(Book)
        assert list(cache) == []
        assert list(cache.keys()) == []
    finally:
        cache.clear()


def test_a_cache_hit_returns_the_stored_value_itself() -> None:
    """Bug: every hit of QUERY_SHAPE_CACHE/DECODE_PLAN_CACHE rebuilt the whole stored value -
    each tuple, list and dataclass inside it - to resolve the weak references it had been stored
    with, the largest single cost of a simple `get()`. A hit is one lookup now."""
    cache: Cache[tuple[Any, ...]] = Cache(max_size=8)
    try:
        value = (Author, ("nested", [1, 2]), Author._meta.fields_map["name"])
        cache[(Author, "shape")] = value
        assert cache[(Author, "shape")] is value
        assert cache.get((Author, "shape")) is value
        assert (Author, "shape") in cache
        assert cache.get((Author, "missing"), "default") == "default"
        assert (Author, "missing") not in cache
    finally:
        cache.clear()


def test_a_value_without_sql_is_served_whichever_builder_the_model_is_bound_to(monkeypatch) -> None:
    """A cache whose values hold no SQL (``holds_sql=False``) keeps a model's entries when the
    model is bound to another query builder; forgetting the model still drops them."""
    cache: Cache[str] = Cache(max_size=8, holds_sql=False)
    sql_cache: Cache[str] = Cache(max_size=8)
    try:
        cache[(Author, "schema")] = "value"
        sql_cache[(Author, "schema")] = "sql"
        # As if Author were bound to another context's builder.
        monkeypatch.setattr(Author._meta, "basequery", QueryBuilder())
        assert cache[(Author, "schema")] == "value"
        assert list(cache) == [(Author, "schema")]
        assert (Author, "schema") not in sql_cache
        cache.forget_model(Author)
        assert (Author, "schema") not in cache
    finally:
        cache.clear()
        sql_cache.clear()


def _walk_reachable_objects(root: Any) -> Any:
    """Yields every object reachable from `root` via tuple/list/set/dict contents or a generic
    object's own `__dict__`, each exactly once (by `id()`). A `type` object is yielded but never
    descended into - its own class body/MRO is irrelevant to what a query's runtime object graph
    actually holds, and recursing into it would explode into unrelated metaclass internals."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        yield obj
        if isinstance(obj, type) or isinstance(obj, (str, bytes, int, float, bool, type(None))):
            continue
        if isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        elif hasattr(obj, "__dict__"):
            stack.extend(vars(obj).values())


@pytest.mark.asyncio
async def test_query_builder_never_embeds_a_model_class_or_field_reference(db):
    """The SQL layer (`hare/sql/`) stays independent of the ORM: a `QueryBuilder` never holds a
    `type[Model]` or `Field`. Nothing structural enforces that - neither module imports
    `hare.models`/`hare.fields` today - so this walks the FULL object graph reachable from a
    richly-populated, real `QueryBuilder` (filter WHERE tree, a select_related() JOIN, an
    order_by()) and fails the moment one becomes reachable from it. A cached shape holding such a
    reference would also keep the class alive until its model's shape caches are dropped.
    """
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    qs = Book.objects.filter(name="alpha").exclude(rating=0.0).select_related("author").order_by("name")
    compiler = qs._get_compiler()
    compiler._apply_connection(qs.get_connection())
    compiler._make_query()

    offenders = [
        obj
        for obj in _walk_reachable_objects(compiler.query)
        if isinstance(obj, ModelField) or (isinstance(obj, type) and issubclass(obj, Model))
    ]
    assert offenders == [], (
        "QueryBuilder now reaches a type[Model]/Field reference - the SQL layer must stay "
        "independent of the ORM's model classes and fields"
    )
