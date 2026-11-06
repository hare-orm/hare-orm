"""Tests for the decode plans (``StatementPlans.decode_plans``) - the cache a model query consults
before calling ``InstanceHydration.build_decode_plan()``, keyed by the model, connection's dialect,
alias and native result types, selected columns and select_related shape. Its result depends on
query shape and reader capabilities, never on filter VALUES, so a
repeated shape must hit the cache (verified via a call-count spy on ``_build_decode_plan`` itself,
not by inspecting the cache directly - that would only prove a key exists, not that the expensive
rebuild was actually skipped), and a genuinely different shape must miss it.
"""

from contextlib import aclosing
from copy import copy
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from hare import Hare, fields
from hare.core.apps import Apps
from hare.core.config import HareConfig
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from hare.models import Model
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.model_rows.combined_model_rows import CombinedModelRows
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.statements.select.model_rows.instance_hydration import InstanceHydration
from tests.shared_models.testmodels import Author, Book
from tests.utils.model_rows_queries import get_model_rows_query


@pytest.fixture(autouse=True)
def clear_decode_plan_cache():
    """The decode plans persist across tests in the same process, and every test below asserts
    an absolute call count on a cold cache. The statement plans are cleared alongside them: a
    query running on a found plan never reaches ``_build_decode_plan()`` either."""
    StatementPlans.decode_plans.clear()
    StatementPlans.plans.clear()
    yield
    StatementPlans.decode_plans.clear()
    StatementPlans.plans.clear()


def _spy_on_build_decode_plan():
    """A patch.object() context manager wrapping the real _build_decode_plan so call count is
    observable without changing its behavior - autospec=True keeps it a proper bound-method
    spy (call_count reflects real invocations, not attribute lookups)."""
    return patch.object(
        InstanceHydration, "build_decode_plan", side_effect=InstanceHydration.build_decode_plan, autospec=True
    )


def _build(queryset):
    """The model query of ``queryset``, built for its connection."""
    compiler = get_model_rows_query(queryset)
    compiler._make_query()
    return compiler


@pytest.mark.database_independent
@pytest.mark.parametrize("cache_path", ["row_columns", "prepared_columns", "decode_plan", "statement", "calls"])
@pytest.mark.parametrize(
    ("field_class", "field_type", "raw", "converted"),
    [(fields.IntField, int, 7, 107), (fields.BooleanField, bool, False, True)],
)
def test_cached_readers_follow_native_result_types(cache_path, field_class, field_type, raw, converted):
    class ConvertedField(field_class):
        keeps_native_db_values = True

        def from_db_value(self, value):
            return converted

    class Number(Model):
        number = ConvertedField()

    module = ModuleType("native_reader_cache_models")
    module.Number = Number
    Hare.bind_models(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module]}))
    source_client = AiosqliteClient(connection_alias="same", file_path=":memory:")
    Apps.bind_model_base_queries(Number, source_client)
    plans = []
    decoded = []
    for native in (False, True, False):
        # No connection is opened. Copies keep every other cache dependency, including the
        # SQLite parameter adapter identity, equal while reader capabilities vary.
        client = copy(source_client)
        client.native_python_types = (
            client.native_python_types | {field_type} if native else client.native_python_types - {field_type}
        )
        query = None
        if cache_path != "row_columns":
            query = get_model_rows_query(Number.objects.all().using(client))
            if cache_path == "decode_plan":
                query._make_query()
            else:
                if cache_path == "statement":
                    query._call_signature = None
                query._make_query_to_run()
        reader = (
            InstanceHydration.get_model_rows_reader(query)
            if cache_path == "prepared_columns"
            else ModelRows(Number, client, decode_plan=query._decode_plan if query is not None else None)
        )
        result = StatementResult(1, [(1, raw)])
        result.description = (("id",), ("number",))
        columns = reader.get_columns(result)
        decoded.append(reader.read_batch(result, columns)[0].number)
        if cache_path in ("row_columns", "prepared_columns"):
            plans.append(columns)
        elif cache_path == "decode_plan":
            plans.append(query._decode_plan)
        else:
            plans.append(query._statement_plan)
    assert decoded == [converted, raw, converted]
    assert plans[0] is not None
    assert plans[0] is plans[2]
    assert plans[0] is not plans[1]


@pytest.fixture
def column_binding_model():
    class Book(Model):
        title = fields.TextField()

    module = ModuleType("column_binding_models")
    module.Book = Book
    Hare.bind_models(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module]}))
    client = AiosqliteClient(connection_alias="same", file_path=":memory:")
    Apps.bind_model_base_queries(Book, client)
    return Book, client


@pytest.mark.database_independent
@pytest.mark.parametrize("by_position", [False, True])
def test_prepared_columns_allow_unhashable_readers_and_follow_invalidation(column_binding_model, by_position):
    model, source = column_binding_model

    class Reader:
        __hash__ = None

        def __init__(self, prefix):
            self.prefix = prefix

        def __call__(self, value):
            return self.prefix + value

    client = SimpleNamespace(
        connection_alias=source.connection_alias,
        dialect=source.dialect,
        native_python_types=source.native_python_types,
        features=SimpleNamespace(supports_positional_rows=by_position),
    )
    field = model._meta.fields_map["title"]

    def read(value, prefix):
        reader = ModelRows(
            model,
            client,
            decode_plan=(("title", field, Reader(prefix)),),
            decode_plan_is_partial=True,
            decode_plan_key=("title",),
        )
        # Positional prepared results need no driver description or keys() method.
        result = StatementResult(1, [(value,)] if by_position else [{"title": value}])
        return reader.read_batch(result, reader.get_columns(result))[0]

    assert read("first", "A:").title == "A:first"
    assert read("second", "A:").title == "A:second"
    StatementPlans.row_columns.forget_holder(model)
    refreshed = read("third", "B:")
    assert refreshed.title == "B:third"
    assert refreshed._partial is True


@pytest.mark.database_independent
def test_explicit_decode_plans_without_a_compiler_key_keep_their_readers(column_binding_model):
    model, client = column_binding_model
    field = model._meta.fields_map["title"]
    result = StatementResult(1, [("mixed",)])
    actual = []
    for convert in (str.upper, str.lower):
        reader = ModelRows(model, client, decode_plan=(("title", field, convert),), decode_plan_is_partial=True)
        actual.append(reader.read_batch(result, reader.get_columns(result))[0].title)
    assert actual == ["MIXED", "mixed"]


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("by_position", [False, True])
@pytest.mark.parametrize("stop_early", [False, True])
@pytest.mark.parametrize("combined", [False, True])
async def test_model_stream_binds_nonempty_batches_and_closes(column_binding_model, by_position, stop_early, combined):
    model, source = column_binding_model
    closed = []
    requests = []

    def make_row(title):
        if combined:
            values = (model._meta.app, model.__name__, title)
            names = (reader.app_column, reader.model_column, "title")

            class DriverRow(tuple):
                def keys(self):
                    return names

            return DriverRow(values) if by_position else dict(zip(names, values, strict=True))
        return (title,) if by_position else {"title": title}

    async def stream_batches(sql, values, *, chunk_size):
        requests.append((sql, values, chunk_size))
        try:
            yield []
            yield [make_row("one")]
            yield []
            yield [make_row("two")]
        finally:
            closed.append(True)

    client = SimpleNamespace(
        connection_alias=source.connection_alias,
        dialect=source.dialect,
        native_python_types=source.native_python_types,
        features=SimpleNamespace(supports_positional_rows=by_position),
        stream_batches=stream_batches,
    )
    if combined:
        reader = CombinedModelRows(model, {model}, ())
        stream = reader.stream_batches(client, "SELECT title", [7], chunk_size=1)
        expected = [[], ["one"], [], ["two"]]
    else:
        reader = ModelRows(
            model,
            client,
            decode_plan=(("title", model._meta.fields_map["title"], str.upper),),
            decode_plan_is_partial=True,
            decode_plan_key=("title",),
        )
        stream = reader.stream_batches("SELECT title", [7], chunk_size=1)
        expected = [[], ["ONE"], [], ["TWO"]]
    batches = []
    async with aclosing(stream) as source_batches:
        async for batch in source_batches:
            assert all(obj._partial for obj in batch)
            batches.append([obj.title for obj in batch])
            if stop_early and batch:
                break
    assert batches == (expected[:2] if stop_early else expected)
    assert closed == [True]
    assert requests == [("SELECT title", [7], 1)]


@pytest.mark.asyncio
async def test_same_shape_repeated_hits_cache(db):
    """Two `.all()` calls on the same model, same columns, same select_related shape (none)
    must only ever call the real _build_decode_plan() once - the second is a pure cache hit."""
    await Author.objects.create(name="a")
    with _spy_on_build_decode_plan() as spy:
        query1 = _build(Author.objects.all())
        query2 = _build(Author.objects.all())
    assert spy.call_count == 1
    assert query1._decode_plan == query2._decode_plan


@pytest.mark.asyncio
async def test_different_only_fields_misses_cache(db):
    """A different selected-column set (.only(...)) is a genuinely different shape - must not
    reuse the other shape's cached plan, and must call _build_decode_plan() again."""
    await Author.objects.create(name="a")
    with _spy_on_build_decode_plan() as spy:
        query_all = _build(Author.objects.all())
        query_only = _build(Author.objects.all().only("id"))
    assert spy.call_count == 2
    assert query_all._decode_plan != query_only._decode_plan
    assert len(query_only._decode_plan) == 1
    assert query_only._decode_plan[0][0] == "id"


@pytest.mark.asyncio
async def test_different_model_misses_cache(db):
    """Two different models never share a cache entry, even with structurally similar column
    sets - the model itself is part of the key."""
    await Author.objects.create(name="a")
    with _spy_on_build_decode_plan() as spy:
        query_author = _build(Author.objects.all())
        query_book = _build(Book.objects.all())
    assert spy.call_count == 2
    plan_names = {name for name, _field, _reader in query_author._decode_plan}
    assert "name" in plan_names  # Author has a `name` column too - confirms it's a real check
    assert query_author._decode_plan != query_book._decode_plan


@pytest.mark.asyncio
async def test_select_related_shape_misses_cache(db):
    """select_related changes the query's join/decode_plan-eligibility shape - a plain .all()
    (decode_plan eligible) and a .select_related(...) query (decode_plan is None - the joined
    rows are read through each model's own columns) must not collide."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="b", author=author, rating=4.5)
    with _spy_on_build_decode_plan() as spy:
        query_plain = _build(Book.objects.all())
        query_joined = _build(Book.objects.all().select_related("author"))
    assert spy.call_count == 2
    assert query_plain._decode_plan is not None
    assert query_joined._decode_plan is None


@pytest.mark.asyncio
async def test_repeated_filter_with_different_values_still_hits_decode_plan_cache(db):
    """The whole point of caching decode_plan separately from the SQL/filter-value machinery:
    two `.filter(name=...)` calls with DIFFERENT values are still the SAME decode_plan shape
    (same model, same selected columns) - must still be a single _build_decode_plan() call,
    and (this being the actual point of a cache) each call must still return its own correct
    row for its own filter value, proving the cached plan is safely reused."""
    await Author.objects.create(name="alice")
    await Author.objects.create(name="bob")
    with _spy_on_build_decode_plan() as spy:
        alice = await Author.objects.filter(name="alice").first()
        bob = await Author.objects.filter(name="bob").first()
    assert spy.call_count == 1
    assert alice.name == "alice"
    assert bob.name == "bob"


@pytest.mark.asyncio
async def test_decode_plan_cache_survives_across_separate_queryset_instances(db):
    """The cache is a cross-instance cache (not scoped to a single QuerySet/connection) - a
    brand new QuerySet instance for the same shape, after the cache is already warm, must still
    be a cache hit."""
    await Author.objects.create(name="a")
    warm_up = _build(Author.objects.all())  # not spied on - just ensures the cache entry exists

    with _spy_on_build_decode_plan() as spy:
        query = _build(Author.objects.all())
    assert spy.call_count == 0
    assert query._decode_plan == warm_up._decode_plan
