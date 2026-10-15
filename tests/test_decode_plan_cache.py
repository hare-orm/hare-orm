"""Tests for the decode plans (``StatementPlans.decode_plans``) - the cache a model query consults
before calling ``_build_decode_plan()``, keyed by (model, dialect, selected-column-names,
select_related shape). Its result is a pure function of query SHAPE, never of filter VALUES, so a
repeated shape must hit the cache (verified via a call-count spy on ``_build_decode_plan`` itself,
not by inspecting the cache directly - that would only prove a key exists, not that the expensive
rebuild was actually skipped), and a genuinely different shape must miss it.
"""

from unittest.mock import patch

import pytest

from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.model_rows.model_columns import ModelColumns
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.query.statements.select.model_rows.instance_hydration import InstanceHydration
from tests.testmodels import Author, Book
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
    plan_names = {name for name, _field, _bucket, _reader in query_author._decode_plan}
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


@pytest.mark.asyncio
async def test_hydrate_function_resolved_once_per_query_not_per_row(db, monkeypatch):
    """The per-row loop must take the compiled row reader once for the whole query, not once
    per row. Forces the pure-Python path - the accelerator reads a whole batch without it, which
    would make this assertion pass for the wrong reason."""
    for i in range(5):
        await Author.objects.create(name=f"author-{i}")
    monkeypatch.setattr(HydrateAccelerator, "module", None)
    with patch.object(
        ModelColumns, "get_hydrate_function", side_effect=ModelColumns.get_hydrate_function, autospec=True
    ) as spy:
        authors = await Author.objects.all()
    assert len(authors) == 5
    assert spy.call_count == 1
