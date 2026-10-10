"""Tests for QuerySet's `QUERY_SHAPE_CACHE` (hare/query/queryset/queryset.py) - the fast path
that reuses a fully-built hare.sql query (selects/orderby/distinct/WHERE all applied) across
repeated calls of the SAME `.filter(...)` shape, binding only the leaf `ValueWrapper` values into
the shape's SQL text (`StatementPlan.bind()`). Unlike `DECODE_PLAN_CACHE` (query SHAPE
only), this cache stores real query structure keyed on filter KEYS - so its primary risk is
silent data corruption: reusing the wrong criterion structure, or a stale/shared value, for a
different call. Every test below therefore checks actual RESULTS, not just cache bookkeeping -
proving the fast path is both taken (via a spy) AND correct (via the returned rows), for the
full matrix the design docs mandate: repeated-different-values, every eligible lookup shape,
every remaining excluded shape (fk-shortcut/CTE/an __in list with an embedded None - OR,
nested/multi-kwarg Q, non-join negation, __in/__not_in, select_related, a
CombinedExpression/Value/F literal annotate(), .after_cursor() keyset pagination, a single-hop
related__field=value join-crossing filter (plain or negated via .exclude()'s NOT EXISTS
rewrite), every __contains/__startswith/__endswith LIKE-family lookup (case-sensitive AND
case-insensitive - the latter's pattern embeds one level deeper, inside an UPPER(...) SQL
function call), JSON __contains/__contained_by (already covered for free, no new code - see
test_json_contains_and_contained_by_already_fast_pathed_and_stay_correct), and ArrayField
__contains/__contained_by/__overlap/__len (the first two/three substitute the WHOLE Array node,
not a leaf value inside it - see ArrayValueReference's own docstring) are now covered by the fast path;
Function/Aggregate/Window/Case annotations, an F() cross-reference to another annotation, a NULL
.after_cursor() boundary value, a multi-value/non-scalar related-field filter
(related__field__in=[...]), and the JSON __filter lookup (its value_encoder is an identity
pass-through on the WHOLE raw dict, but the criterion it builds embeds a decomposed PIECE of that
dict instead) stay excluded), None/isnull's special rewrite, dialect non-collision, and concurrent
access. Also covers `register_lookup()`-registered custom filters generically (no lookup-specific
code) - a lookup whose own operator embeds `encoded_value` unchanged is automatically eligible
through the SAME ScalarValueReference/RangeValueReference/ListValueReference mechanisms every built-in lookup uses,
gated by an `is` identity check against the encoder's own direct output (not just a structural
isinstance check) - this identity check was ADDED to those three ref shapes after a live-verified
silent-wrong-result bug: a custom lookup whose operator TRANSFORMS the value (e.g. reverses it)
used to be wrongly treated as a plain reusable scalar, substituting `to_db_value(new_raw)` in
place of whatever the operator actually embedded.
"""

import asyncio
import datetime
import os
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.dialect_registry import DialectRegistry
from hare.fields import CharField
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Case, F, Q, RawSQL, Value, When, Window
from hare.query.expressions.subqueries.exists import Exists
from hare.query.expressions.subqueries.outer_reference import OuterReference
from hare.query.filters import FieldLookup
from hare.query.functions import Avg, Coalesce, Count, Max, Min, Sum
from hare.query.functions.window import Lag, NTile, RowNumber, Sum as WindowSum
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.relation_loading.select import Select
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.select.values_query import ValuesQuery
from hare.query.statements.summary.aggregate_query import AggregateQuery
from hare.query.statements.summary.contains_query import ContainsQuery
from hare.query.statements.summary.count_query import CountQuery
from hare.query.statements.summary.exists_query import ExistsQuery
from hare.sql.terms import Criterion, Not, Term, ValueWrapper
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from tests.testmodels import (
    Author,
    Book,
    Category,
    CharFields,
    DateFields,
    DatetimeFields,
    DecimalFields,
    DoubleFK,
    Event,
    IntFields,
    JSONFields,
    LazyJoinedChild,
    LazyJoinedParent,
    LazySelectChild,
    LazySelectParent,
    Reporter,
    TenantScopedOrder,
    TenantScopedWidget,
    Tournament,
)
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.timezone_context import override_timezone

# Counts the builds a run on a plan saves - plan verification compares after the body.
pytestmark = pytest.mark.plan_verification_after_test


def _built(queryset):
    """The query ``queryset`` runs, bound to its connection and built - what a run builds."""
    compiler = queryset._get_compiler()._get_execution_query()
    compiler._make_query()
    return compiler


def _safe_custom_lookup(field: CharField | None) -> FieldLookup:
    """A `register_lookup()`-registered custom filter whose operator does NOT transform the
    value - just an ordinary equality comparison, functionally identical to the built-in bare
    `field=value` shape. Registered here to prove custom lookups CAN be safely cached when their
    own operator doesn't do this (contrast with `_unsafe_custom_lookup` below)."""

    def _operator(term: Term, value: str) -> Term:
        return term == value

    return FieldLookup(_operator)


def _unsafe_custom_lookup(field: CharField | None) -> FieldLookup:
    """A `register_lookup()`-registered custom filter whose operator DOES transform the value
    (reverses it) before comparing - the exact shape that caught a real silent-wrong-result bug
    in `ScalarValueReference`'s own identity check (see `test_unsafe_custom_lookup_stays_excluded_from_
    fast_path`'s own docstring) during this increment's implementation."""

    def _operator(term: Term, value: str) -> Term:
        return term == value[::-1]

    return FieldLookup(_operator)


def _rebuilt_custom_lookup(field: CharField | None) -> FieldLookup:
    """The transforming custom filter of `_unsafe_custom_lookup`, declaring that its SQL text depends
    on nothing of the value but its type - a plan builds its criterion again from a later value."""

    def _operator(term: Term, value: str) -> Term:
        return term == value[::-1]

    return FieldLookup(_operator, binds_by_rebuild=True)


CharField.register_lookup("safe_custom", _safe_custom_lookup)
CharField.register_lookup("unsafe_custom", _unsafe_custom_lookup)
CharField.register_lookup("rebuilt_custom", _rebuilt_custom_lookup)


def _value_wrapper_ids(criterion: Criterion | None) -> set[int]:
    """Every ``ValueWrapper`` leaf's ``id()`` reachable from a WHERE criterion tree - the
    unambiguous signal for the symmetry-principle invariant: a query built in full must never
    hold a node of the plan's own template."""
    if not criterion:
        return set()
    return {id(node) for node in criterion.nodes_() if isinstance(node, ValueWrapper)}


@pytest.fixture(autouse=True)
def clear_query_shape_cache():
    """Module-level and never invalidated in production (same policy as DECODE_PLAN_CACHE) -
    must be cleared between tests or an earlier test's cached shape silently answers a later
    test's "cold cache" assumptions with stale data."""
    StatementPlans.plans.clear()
    yield
    StatementPlans.plans.clear()


@pytest_asyncio.fixture(scope="function")
async def db_array_fields():
    """`ArrayField` lives in `tests.testmodels_postgres`, a separate model module from the
    default `tests.testmodels` the shared `db` fixture (and every other test in this file) uses -
    mirrors `tests/fields/conftest.py`'s own `db_array_fields` fixture, which isn't visible here
    (sibling directory, not an ancestor of this file - pytest conftest fixtures don't cross that
    boundary) rather than importing it directly."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("ArrayField requires PostgreSQL")
    async with hare_test_context(
        modules=["tests.testmodels_postgres"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


def _spy_on_plan_hits():
    """StatementPlans.count_hit() runs only when a query runs on its shape's plan - its call
    count is the one unambiguous signal that the fast path actually activated, as opposed to
    every call happening to take the normal full-rebuild path."""
    return patch.object(StatementPlans, "count_hit", side_effect=StatementPlans.count_hit)


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)


def _spy_on_plan_hits_of_any_type():
    """Same as `_spy_on_plan_hits()` - for `ExistsQuery`/`CountQuery`, siblings of `QuerySet`."""
    return patch.object(StatementPlans, "count_hit", side_effect=StatementPlans.count_hit)


@pytest.mark.asyncio
async def test_repeated_shape_is_a_cache_hit_on_second_call(db):
    await Author.objects.create(name="a")
    with _spy_on_plan_hits() as spy:
        await Author.objects.filter(name="a").order_by("id")
        assert spy.call_count == 0  # first call: cache miss, no plan yet
        await Author.objects.filter(name="a").order_by("id")
        # A nonzero count is the signal the fast path actually activated (see
        # _spy_on_plan_hits()'s own docstring).
        assert spy.call_count > 0  # second call: cache hit


@pytest.mark.asyncio
async def test_repeated_filter_different_values_returns_correct_independent_results(db):
    """The primary risk scenario: same shape, different values, twice - a shared/stale
    ValueWrapper would make the second call return the first call's row (or nothing)."""
    await Author.objects.create(name="alice")
    await Author.objects.create(name="bob")
    with _spy_on_plan_hits() as spy:
        first_call = (await Author.objects.filter(name="alice").order_by("id"))[0]
        second_call = (await Author.objects.filter(name="bob").order_by("id"))[0]
    assert spy.call_count > 0
    assert first_call.name == "alice"
    assert second_call.name == "bob"


@pytest.mark.asyncio
async def test_limit_and_offset_are_eligible_for_the_fast_path(db):
    """LIMIT/OFFSET don't embed a runtime value into the WHERE-tree substitution mechanism this
    cache is built around - they're applied on top of a cache hit exactly like `.first()`'s own
    implicit `_limit=1` now is, unconditionally overwritten from THIS call's own
    `self._limit`/`self._offset` regardless of what (if anything) got baked into the cached
    template the first time this shape was seen. This is the single most common real-world query
    shape (filter + order_by + limit/offset - pagination) - excluding it from the cache meant
    every page of every paginated list rebuilt filters/ordering from scratch."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            assert (await Book.objects.filter(name="alpha").first()).name == "alpha"
    assert spy.call_count > 0  # first call: cache miss; 2 more: cache hits

    # LIMIT/OFFSET are deliberately NOT part of plan_key (same as filter values) - a
    # plain `.all().order_by("name")` shares its cache entry regardless of what limit/offset is
    # applied afterward, so each block below clears the cache first to stay independent/
    # order-proof rather than relying on (or asserting) that sharing as this test's own subject.
    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = [b.name async for b in Book.objects.all().order_by("name").limit(1)]
            assert names == ["alpha"]
    assert spy.call_count > 0

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = [b.name async for b in Book.objects.all().order_by("name").offset(1)]
            assert names == ["beta"]
    assert spy.call_count > 0

    # Different limit/offset values, same underlying filter/ordering shape - the cached
    # template must never leak one call's limit/offset into another's.
    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first_page = [b.name async for b in Book.objects.all().order_by("name").limit(1).offset(0)]
        second_page = [b.name async for b in Book.objects.all().order_by("name").limit(1).offset(1)]
    assert spy.call_count > 0  # first call: miss; second: hit, same shape, different limit/offset
    assert first_page == ["alpha"]
    assert second_page == ["beta"]


@pytest.mark.asyncio
async def test_order_by_is_part_of_the_cache_key(db):
    """order_by() on a plain field never builds a ValueWrapper (get_ordering() is purely
    structural - field name + Order enum + optional dialect cast function), so it was already
    fully cacheable before this file's own increments began - `tuple(self._orderings)` has
    always been part of plan_key. This is a dedicated regression test for that (previously
    only incidentally exercised alongside limit/offset elsewhere in this file): a different
    order_by field OR direction on the SAME filter shape must be a SEPARATE cache entry, never
    silently reuse another ordering's template."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = [b.name async for b in Book.objects.filter(rating__gt=0).order_by("name").all()]
        assert spy.call_count == 0
        second = [b.name async for b in Book.objects.filter(rating__gt=0).order_by("name").all()]
    assert spy.call_count > 0
    assert first == ["alpha", "beta"]
    assert second == ["alpha", "beta"]
    assert len(StatementPlans.plans) == 1

    reverse = [b.name async for b in Book.objects.filter(rating__gt=0).order_by("-name").all()]
    assert reverse == ["beta", "alpha"]
    assert len(StatementPlans.plans) == 2, "a different order_by direction must be a separate cache entry"

    await Book.objects.filter(rating__gt=0).order_by("rating").all()
    assert len(StatementPlans.plans) == 3, "a different order_by field must be a separate cache entry"


@pytest.mark.asyncio
async def test_order_by_null_placement_is_part_of_the_cache_key(db):
    """The same field and direction with a different explicit NULL placement (NULLS FIRST/NULLS LAST/
    dialect default) is a SEPARATE cache entry - reusing another placement's template would silently
    return the rows in the wrong order."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)

    async def numbers(ordering):
        return [row.intnum for row in await IntFields.objects.filter(intnum__gt=0).order_by(ordering, "intnum")]

    nulls_first = F("intnum_null").asc(nulls_first=True)
    nulls_last = F("intnum_null").asc(nulls_last=True)
    assert await numbers(nulls_first) == [2, 4, 1, 3]
    assert len(StatementPlans.plans) == 1
    assert await numbers(nulls_last) == [1, 3, 2, 4]
    assert len(StatementPlans.plans) == 2, "NULLS LAST must not reuse the NULLS FIRST template"
    await numbers(F("intnum_null").asc())
    assert len(StatementPlans.plans) == 3, "the dialect default must not reuse an explicit placement's template"
    await numbers(F("intnum_null").desc(nulls_last=True))
    assert len(StatementPlans.plans) == 4

    with _spy_on_plan_hits() as spy:
        assert await numbers(nulls_first) == [2, 4, 1, 3]
        assert await numbers(nulls_last) == [1, 3, 2, 4]
    assert spy.call_count == 2, "repeating each placement still hits its own cache entry"
    assert len(StatementPlans.plans) == 4


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_now_fast_pathed(db):
    """select_for_update()'s five flags are pure DDL, fixed at the call site - no substitution
    needed, just inclusion in plan_key (see _query_is_plannable()'s own docstring).
    Two different filter VALUES for the SAME lock-mode shape must reuse one
    cache entry; a DIFFERENT lock-mode flag (nowait) for the SAME filter shape must be a separate
    one, not silently reuse/corrupt the plain FOR UPDATE template."""
    qs_first = Book.objects.filter(name="alpha")
    qs_first._select_for_update = True
    key_before = set(StatementPlans.plans)
    qs_first.sql()
    assert len(StatementPlans.plans) == len(key_before) + 1

    qs_second = Book.objects.filter(name="beta")
    qs_second._select_for_update = True
    qs_second.sql()
    assert len(StatementPlans.plans) == len(key_before) + 1, "same lock shape must reuse one entry"

    qs_nowait = Book.objects.filter(name="alpha")
    qs_nowait._select_for_update = True
    qs_nowait._select_for_update_nowait = True
    qs_nowait.sql()
    assert len(StatementPlans.plans) == len(key_before) + 2, "a different lock mode must be a separate entry"
    assert "NOWAIT" in qs_nowait.sql().upper()
    assert "NOWAIT" not in qs_first.sql().upper()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup,value,expected",
    [
        ("rating", 1.0, ["alpha"]),
        ("rating__gt", 0.5, ["alpha", "beta"]),
        ("rating__gte", 1.0, ["alpha", "beta"]),
        ("rating__lt", 5.0, ["alpha"]),
        ("rating__lte", 5.0, ["alpha", "beta"]),
    ],
)
async def test_every_eligible_lookup_hits_the_fast_path_and_stays_correct(db, lookup, value, expected):
    """Every plain-comparison lookup (no value_encoder, no join, a bare BasicCriterion+
    ValueWrapper shape) is eligible for the fast path - each must give the SAME result with the
    cache warm as it would cold, and must actually take it (spy.call_count > 0)."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)

    cold = await Book.objects.filter(**{lookup: value}).order_by("name").all()
    with _spy_on_plan_hits() as spy:
        warm = await Book.objects.filter(**{lookup: value}).order_by("name").all()
    assert spy.call_count > 0
    assert [b.name for b in warm] == [b.name for b in cold]
    assert [b.name for b in warm] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup,value,expected",
    [
        ("name__not", "beta", ["alpha"]),
    ],
)
async def test_lookups_with_or_shapes_run_on_their_plan_and_stay_correct(db, lookup, value, expected):
    """`__not` resolves to an OR of two criteria (`field.ne(value) | field.isnull()`) holding the
    value as its one parameter - every repeat after the first runs on the plan."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = [b.name for b in await Book.objects.filter(**{lookup: value}).order_by("name").all()]
            assert names == expected
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_none_value_after_cache_warm_uses_isnull_not_a_stale_equality(db):
    """`.filter(subject=None)` rewrites to an `IS NULL` criterion (Q._process_filter_kwarg's
    isnull special case) - a completely different shape than the `=` criterion a non-None
    `.filter(subject=...)` call caches. A hit that naively rebound None into the cached `=`
    ValueWrapper would produce `subject = NULL` (matches nothing, ever) instead of `IS NULL`."""
    author = await Author.objects.create(name="a")
    with_subject = await Book.objects.create(name="has-subject", author=author, rating=1.0, subject="fiction")
    await Book.objects.create(name="no-subject", author=author, rating=2.0, subject=None)

    # Warm the cache for the "subject" shape with a non-None value first.
    warmed = await Book.objects.filter(subject="fiction").first()
    assert warmed.id == with_subject.id

    none_result = await Book.objects.filter(subject=None).first()
    assert none_result is not None
    assert none_result.name == "no-subject"


@pytest.mark.asyncio
async def test_or_combination_now_fast_pathed_and_stays_correct(db):
    """A plan binds the values of `ComplexCriterion` (the node `Q.__or__` produces) at any
    depth, and `Q._get_kwargs()` already records one
    `value_wrapper_references` entry per leaf kwarg regardless of tree shape - `_query_shape_is_
    cacheable()`'s recursive `_q_node_is_cacheable()` now allows OR through. Two DIFFERENT
    value pairs across repeated calls must still each get their own correct, non-stale rows."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(Q(name="alpha") | Q(name="gamma")).all()}
        assert spy.call_count == 0  # first call: miss, no plan yet
        second = {b.name async for b in Book.objects.filter(Q(name="alpha") | Q(name="beta")).all()}
    # second call: hit - >0 is the "took the fast path" signal, same convention as the
    # concurrent-calls test below.
    assert spy.call_count > 0
    assert first == {"alpha", "gamma"}
    assert second == {"alpha", "beta"}
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_nested_q_now_fast_pathed_and_stays_correct(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(Q(Q(name="alpha"))).all()}
        second = {b.name async for b in Book.objects.filter(Q(Q(name="beta"))).all()}
    assert spy.call_count > 0
    assert first == {"alpha"}
    assert second == {"beta"}


@pytest.mark.asyncio
async def test_multi_kwarg_single_q_now_fast_pathed_and_stays_correct(db):
    """`Q(a=1, b=2)` - two kwargs on the SAME `Q` node, ANDed together - used to be excluded by
    the old `len(q.filters) == 1` check; the recorder already looped `self.filters.items()` for
    every kwarg regardless, so the only change needed was the pre-check/key/value-collection
    logic, not the recording itself."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="alpha", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(Q(name="alpha", rating=1.0)).all()
        assert spy.call_count == 0  # first call: miss
        second = await Book.objects.filter(Q(name="alpha", rating=2.0)).all()
    assert spy.call_count > 0  # second call: hit - two leaves means more than one clone call
    assert [b.rating for b in first] == [1.0]
    assert [b.rating for b in second] == [2.0]


@pytest.mark.asyncio
async def test_deeply_nested_and_inside_or_now_fast_pathed_and_stays_correct(db):
    """`(Q(a) & Q(b)) | Q(c)` - a 3-level tree (OR at the root, an AND pair nested under one of
    its branches) - exercises `_q_tree_shape_key()`/`_q_tree_leaf_values()`'s recursion past a
    single level, not just a flat OR of two leaves."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)
    await Book.objects.create(name="delta", author=author, rating=4.0)

    def shape(name1: str, rating1: float, name2: str) -> Q:
        return Q(Q(name=name1) & Q(rating=rating1)) | Q(name=name2)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(shape("alpha", 1.0, "gamma")).all()}
        assert spy.call_count == 0
        second = {b.name async for b in Book.objects.filter(shape("beta", 2.0, "delta")).all()}
    assert spy.call_count > 0
    assert first == {"alpha", "gamma"}
    assert second == {"beta", "delta"}
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_tree(db):
    """The symmetry-principle regression test: after a cache HIT for an OR/nested shape, walk the
    freshly built WHERE tree and confirm none of its `ValueWrapper` leaves are the SAME objects
    (by `id()`) as the cached template's own leaves - a shared object here would mean two
    concurrent/sequential calls silently alias each other's filter values."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    qs_first = Book.objects.filter(Q(name="alpha") | Q(name="gamma"))
    await qs_first.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids  # sanity: the OR shape really did populate the cache with real leaves

    qs_second = Book.objects.filter(Q(name="alpha") | Q(name="beta"))
    built_qs_second = _built(qs_second)
    new_ids = _value_wrapper_ids(built_qs_second.query._wheres)

    assert new_ids.isdisjoint(cached_ids)


@pytest.mark.asyncio
async def test_non_join_negated_child_deep_inside_or_tree_now_fast_pathed(db):
    """`_q_node_is_cacheable()` recurses into every child - a NON-join negated node anywhere in
    the tree (not just a negated top-level Q, already covered by `.exclude()`) is a fine candidate:
    a plan binds the values under a `Not(...)` like any other."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(~Q(name="alpha") | Q(name="beta")).all()}
        assert spy.call_count == 0
        second = {b.name async for b in Book.objects.filter(~Q(name="beta") | Q(name="gamma")).all()}
    assert spy.call_count > 0
    assert first == {"beta", "gamma"}
    assert second == {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_join_crossing_negated_child_deep_inside_or_tree_now_fast_pathed(db):
    """The join-crossing counterpart of the test above - a negated child that crosses a relation
    (not just a plain field), buried inside an OR-tree rather than at the Q's own top level, is
    now ALSO eligible: the NestedValueRef recording happens at the leaf (wherever the kwarg
    actually is), independent of tree depth/position, and the cloner's `ComplexCriterion` branch
    already recurses generically into whichever side holds the `Not(ExistsTerm(...))`."""
    author_one = await Author.objects.create(name="a1")
    author_two = await Author.objects.create(name="a2")
    await Book.objects.create(name="alpha", author=author_one, rating=1.0)
    await Book.objects.create(name="beta", author=author_two, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = {b.name async for b in Book.objects.filter(~Q(author__name="a1") | Q(name="nonexistent")).all()}
            assert names == {"beta"}
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_exclude_now_fast_pathed_and_stays_correct(db):
    """A non-join `.exclude(...)`/`~Q(...)` resolves to a single `Not(...)` wrapping the plain
    criterion (`Term.negate()`'s default, see `_finalize_modifier()`) - the cloner's `Not` branch
    descends past it to the same substitutable `ValueWrapper` leaf underneath. Two DIFFERENT
    excluded values across repeated calls must still each get their own correct results."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.all().exclude(name="alpha")}
        assert spy.call_count == 0  # first call: miss
        second = {b.name async for b in Book.objects.all().exclude(name="gamma")}
    assert spy.call_count > 0  # second call: hit
    assert first == {"beta", "gamma"}
    assert second == {"alpha", "beta"}
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_double_negation_now_fast_pathed_and_stays_correct(db):
    """`~Q(~Q(...))` - two SEPARATE Q objects each independently negated (not the single-object
    `~~q` restore case) - resolves to `Not(Not(BasicCriterion(...)))`, a doubly-nested wrap the
    cloner's `Not` branch must recurse through twice, not just once."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(~Q(~Q(name="alpha"))).all()}
        assert spy.call_count == 0
        second = {b.name async for b in Book.objects.filter(~Q(~Q(name="beta"))).all()}
    assert spy.call_count > 0
    assert first == {"alpha"}
    assert second == {"beta"}


@pytest.mark.asyncio
async def test_exclude_nullable_column_now_fast_pathed_and_stays_correct(db):
    """`.exclude(subject=...)` over a direct nullable column resolves through QueryModifier.
    __invert__()'s IS NOT TRUE rewrite (see its own docstring) - a brand new criterion shape
    (`IsNotTrueCriterion`) a cache HIT once didn't know how to reach into, reusing the FIRST
    call's stale bound value for every later call instead of the fresh one - live-verified: this
    exact test caught it (Postgres only; sqlite's `db` fixture isolates each test into its own
    connection/table content, so the collision two DIFFERENT tests sharing one Category/Book row
    set on Postgres exposed never arose there)."""
    author = await Author.objects.create(name="a")
    no_subject = await Book.objects.create(name="no-subject", author=author, rating=1.0, subject=None)
    alpha = await Book.objects.create(name="alpha", author=author, rating=2.0, subject="alpha-subject")
    beta = await Book.objects.create(name="beta", author=author, rating=3.0, subject="beta-subject")

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.all().exclude(subject="alpha-subject")}
        assert spy.call_count == 0  # first call: miss
        second = {b.name async for b in Book.objects.all().exclude(subject="beta-subject")}
    assert spy.call_count > 0  # second call: hit
    assert first == {no_subject.name, beta.name}
    assert second == {no_subject.name, alpha.name}
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_join_crossing_exclude_now_fast_pathed_and_stays_correct(db):
    """`.exclude(author__name=...)` crosses a relation - `_finalize_modifier()` rewrites it to a
    correlated `NOT EXISTS` subquery (`_negate_across_joins()`). Now eligible: `Q._get_nested_
    filter()` threads `value_wrapper_references` into its own recursive resolve on the related model,
    folding a single resulting `ScalarValueReference` into a `NestedValueRef` for the outer kwarg entry,
    and the cloner's `ExistsTerm` branch (reached through `Not`'s existing one) rebinds the
    correlated subquery's own `_wheres`/`_joins` - see both docstrings for the full mechanism."""
    author_one = await Author.objects.create(name="a1")
    author_two = await Author.objects.create(name="a2")
    await Book.objects.create(name="alpha", author=author_one, rating=1.0)
    await Book.objects.create(name="beta", author=author_two, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = {b.name async for b in Book.objects.all().exclude(author__name="a1")}
            assert names == {"beta"}
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_negated_tree(db):
    """The symmetry-principle regression test for negation specifically: after a cache HIT on a
    `~Q(...)` shape, the freshly built `Not(...)`-wrapped tree must not share any `ValueWrapper`
    leaf (by `id()`) with the cached template's own tree."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    qs_first = Book.objects.filter(~Q(name="alpha"))
    await qs_first.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids

    qs_second = Book.objects.filter(~Q(name="beta"))
    built_qs_second = _built(qs_second)
    new_ids = _value_wrapper_ids(built_qs_second.query._wheres)

    assert new_ids.isdisjoint(cached_ids)


@pytest.mark.asyncio
async def test_in_now_fast_pathed_and_stays_correct(db):
    """`__in` on a plain field records a `ListValueReference` (the `Tuple(...)` container `is_in()`
    built) instead of the scalar `ScalarValueReference` - two calls with the SAME list LENGTH but
    DIFFERENT values must reuse one cache entry and stay correct; the list's length is part of
    `plan_key` (see `_leaf_value_shape()`), so a DIFFERENT length is a separate entry."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)
    await Book.objects.create(name="delta", author=author, rating=4.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(name__in=["alpha", "gamma"]).all()}
        assert spy.call_count == 0
        second = {b.name async for b in Book.objects.filter(name__in=["beta", "delta"]).all()}
    assert spy.call_count > 0
    assert first == {"alpha", "gamma"}
    assert second == {"beta", "delta"}
    assert len(StatementPlans.plans) == 1

    third = {b.name async for b in Book.objects.filter(name__in=["alpha", "beta", "gamma"]).all()}
    assert third == {"alpha", "beta", "gamma"}
    assert len(StatementPlans.plans) == 2, "a different list length must be a SEPARATE cache entry"


@pytest.mark.asyncio
async def test_not_in_now_fast_pathed_and_stays_correct(db):
    """`__not_in` always OR's in a `field.isnull()` check for a no-None list (Django-style: NULL
    rows are never excluded by `NOT IN`) - `.find_()` locates the nested `ContainsCriterion`
    inside that `ComplexCriterion(OR, ...)` the same way it does the bare shape `__in` produces."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)
    await Book.objects.create(name="delta", author=author, rating=4.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(name__not_in=["alpha", "gamma"]).all()}
        assert spy.call_count == 0
        second = {b.name async for b in Book.objects.filter(name__not_in=["beta", "delta"]).all()}
    assert spy.call_count > 0
    assert first == {"beta", "delta"}
    assert second == {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_in_with_embedded_none_runs_on_its_plan(db):
    """A `None` inside an `__in`/`__not_in` list ORs in an extra `field.isnull()` check - the plan
    key holds whether the list embeds one, with the number of the other values, which bind."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="has-subject", author=author, rating=1.0, subject="fiction")
    await Book.objects.create(name="no-subject", author=author, rating=2.0, subject=None)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = {b.name async for b in Book.objects.filter(subject__in=["fiction", None]).all()}
            assert names == {"has-subject", "no-subject"}
    assert spy.call_count == 2
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_in_empty_list_runs_on_its_plan_and_stays_correct(db):
    """An empty `__in=[]` resolves to a constant `1=0` `BasicCriterion` (`is_in()`'s own
    degenerate case) binding nothing - a later query of the same plan key runs on the plan."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = [b.name async for b in Book.objects.filter(name__in=[]).all()]
            assert names == []
    assert spy.call_count == 2
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_in_list_tree(db):
    """The symmetry-principle regression test for `__in` specifically: after a cache HIT, none of
    the freshly built `Tuple(...)` container's `ParameterizedValueWrapper` leaves may share an
    `id()` with the CACHED template's own container - and the cached template's own container
    must stay untouched (never mutated in place) after serving a hit."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)
    await Book.objects.create(name="delta", author=author, rating=4.0)

    qs_first = Book.objects.filter(name__in=["alpha", "gamma"])
    await qs_first.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids

    qs_second = Book.objects.filter(name__in=["beta", "delta"])
    built_qs_second = _built(qs_second)
    new_ids = _value_wrapper_ids(built_qs_second.query._wheres)

    assert new_ids.isdisjoint(cached_ids)
    assert _value_wrapper_ids(cached_query._wheres) == cached_ids, "the cached template must never be mutated"


@pytest.mark.asyncio
async def test_range_now_fast_pathed_and_stays_correct(db):
    """A fully-bounded `__range` resolves to a single `BetweenCriterion` (`field.between(lower,
    upper)`, only produced when NEITHER bound is None) - two calls with DIFFERENT bounds must
    reuse one cache entry and each get their own correct result."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)
    await Book.objects.create(name="delta", author=author, rating=4.0)

    with _spy_on_plan_hits() as spy:
        first = {b.name async for b in Book.objects.filter(rating__range=(0.5, 2.5)).all()}
        assert spy.call_count == 0
        second = {b.name async for b in Book.objects.filter(rating__range=(2.5, 4.5)).all()}
    assert spy.call_count > 0
    assert first == {"alpha", "beta"}
    assert second == {"gamma", "delta"}
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_one_sided_range_runs_on_its_plan_and_stays_correct(db):
    """A one-sided `__range` (`(None, upper)`/`(lower, None)`) is a plain `<=`/`>=` comparison - its
    plan key holds the open side, and a later call open on the same side binds its bound."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_plan_hits() as spy:
        for upper, expected in ((1.5, {"alpha"}), (2.5, {"alpha", "beta"}), (0.5, set())):
            names = {b.name async for b in Book.objects.filter(rating__range=(None, upper)).all()}
            assert names == expected
        for lower, expected in ((1.5, {"beta", "gamma"}), (2.5, {"gamma"})):
            names = {b.name async for b in Book.objects.filter(rating__range=(lower, None)).all()}
            assert names == expected
    # The second and third upper bound, the second lower bound.
    assert spy.call_count == 3


@pytest.mark.asyncio
async def test_fully_open_range_runs_on_its_plan_and_stays_correct(db):
    """Both bounds `None` is an `IS NOT NULL` test binding nothing - a plan of its own."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            names = {b.name async for b in Book.objects.filter(rating__range=(None, None)).all()}
            assert names == {"alpha", "beta"}
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_ranges_open_on_different_sides_keep_plans_of_their_own(db):
    """A fully-bounded `BETWEEN`, a range open below, one open above and one open on both sides
    of the same key - four plans, none bound with the values of another."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    calls = [
        ((0.5, 2.5), {"alpha", "beta"}),
        ((None, 1.5), {"alpha"}),
        ((1.5, None), {"beta", "gamma"}),
        ((None, None), {"alpha", "beta", "gamma"}),
    ]
    for _ in range(2):
        for bounds, expected in calls:
            assert {b.name async for b in Book.objects.filter(rating__range=bounds).all()} == expected
    assert len(StatementPlans.plans) == 4


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_range_tree(db):
    """The symmetry-principle regression test for `__range`: after a cache HIT, neither the fresh
    lower nor upper `ValueWrapper` may share an `id()` with the CACHED template's own bounds."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)
    await Book.objects.create(name="delta", author=author, rating=4.0)

    qs_first = Book.objects.filter(rating__range=(0.5, 2.5))
    await qs_first.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids

    qs_second = Book.objects.filter(rating__range=(2.5, 4.5))
    built_qs_second = _built(qs_second)
    new_ids = _value_wrapper_ids(built_qs_second.query._wheres)

    assert new_ids.isdisjoint(cached_ids)
    assert _value_wrapper_ids(cached_query._wheres) == cached_ids


@pytest.mark.asyncio
async def test_fk_relational_shortcut_runs_on_its_plan_and_stays_correct(db):
    """`.filter(author=<instance>)` compares the FK column (author_id) with the instance's key - a
    later instance binds as its key on the same plan (`RelatedKeyValueReference`), never as the instance
    itself."""
    author_one = await Author.objects.create(name="a")
    author_two = await Author.objects.create(name="b")
    await Book.objects.create(name="alpha", author=author_one, rating=1.0)
    await Book.objects.create(name="beta", author=author_two, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(author=author_one).first()
        second = await Book.objects.filter(author=author_two).first()
    assert spy.call_count == 1
    assert first.name == "alpha"
    assert second.name == "beta"


@pytest.mark.asyncio
async def test_select_related_now_fast_pathed_and_stays_correct(db):
    """Plain `select_related("path")` (no `extra_condition`) is purely structural - the JOIN ON
    criteria come entirely from FK metadata, never a runtime value - so it's now eligible for the
    fast path. This is the symmetry-principle regression test for a bug found DURING this
    increment's own implementation, not just a cache-hit/miss check: `_select_related_idx`
    (needed by `_build_select_executor()` to construct an executor that knows about the joined
    columns at all) used to never be restored on a cache hit at all - the SQL had the JOIN, but
    the Python-side row hydration had no idea, silently reading garbage/crashing. Two calls with
    DIFFERENT filter VALUES on the SAME select_related() shape must each correctly resolve their
    OWN related object, not the first call's."""
    author_one = await Author.objects.create(name="a1")
    author_two = await Author.objects.create(name="a2")
    await Book.objects.create(name="alpha", author=author_one, rating=1.0)
    await Book.objects.create(name="beta", author=author_two, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name="alpha").select_related("author").first()
        assert spy.call_count == 0
        second = await Book.objects.filter(name="beta").select_related("author").first()
    assert spy.call_count > 0
    assert first.author.name == "a1"
    assert second.author.name == "a2"
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_select_related_with_only_now_fast_pathed_and_stays_correct(db):
    """`.only()` + `select_related()` combined - `_join_select_related()`'s own related-column
    inclusion logic branches on `self._fields_for_select` directly, not just on the base-table
    columns it resolves to, so `plan_key` includes `self._fields_for_select` itself
    defensively alongside the select_related path set."""
    author_one = await Author.objects.create(name="a1")
    author_two = await Author.objects.create(name="a2")
    await Book.objects.create(name="alpha", author=author_one, rating=1.0)
    await Book.objects.create(name="beta", author=author_two, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name="alpha").only("id", "name").select_related("author").first()
        assert spy.call_count == 0
        second = await Book.objects.filter(name="beta").only("id", "name").select_related("author").first()
    assert spy.call_count > 0
    assert first.author.name == "a1"
    assert second.author.name == "a2"


@pytest.mark.asyncio
async def test_different_select_related_sets_are_separate_cache_entries(db):
    """Two calls sharing the SAME filter shape but different select_related() relation sets
    (including "no select_related at all") must never collide into one cache entry -
    self._decode_plan_key alone doesn't distinguish them (it only reflects the base table's own
    selected columns, snapshotted before select_related joins run), so the relation-path set has
    its own plan_key component."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    await Book.objects.filter(rating__gt=0).select_related("author").all()
    assert len(StatementPlans.plans) == 1

    await Book.objects.filter(rating__gt=0).all()
    assert len(StatementPlans.plans) == 2, "no-select_related must be a separate entry from select_related"


@pytest.mark.asyncio
async def test_select_related_extra_condition_now_fast_pathed_and_stays_correct(db):
    """`Select(relation, extra_condition=Q(...))` resolves `extra_condition` through the
    identical `Q` machinery as any other filter, recorded into the SAME shared `value_wrapper_
    refs` list `_join_table_by_field()` now threads through - two calls with the SAME
    select_related shape but DIFFERENT `extra_condition` VALUES must reuse one cache entry and
    each correctly rebind their own value into the JOIN's own ON criterion, not the other's."""
    author = await Author.objects.create(name="a1")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    with _spy_on_plan_hits() as spy:
        first = (
            await Book.objects.filter(name="alpha")
            .select_related(Select("author", extra_condition=Q(name="a1")))
            .first()
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.filter(name="alpha")
            .select_related(Select("author", extra_condition=Q(name="a2")))
            .first()
        )
    assert spy.call_count > 0
    assert first.author is not None
    assert first.author.name == "a1"
    # extra_condition filters the JOIN itself (LEFT JOIN), not the row - a non-matching value
    # means .author resolves to None (no matching related row), not an exception.
    assert second.author is None
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_select_related_extra_condition_with_only_binds_each_calls_condition(db):
    """`.only()` naming the base model's own fields beside a `select_related()` with an
    `extra_condition`: the plan binds each call's own extra_condition value - two calls with
    different values each get their own result."""
    author_one = await Author.objects.create(name="a1")
    author_two = await Author.objects.create(name="a2")
    await Book.objects.create(name="alpha", author=author_one, rating=1.0)
    await Book.objects.create(name="beta", author=author_two, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            first = (
                await Book.objects.filter(name="alpha")
                .select_related(Select("author", extra_condition=Q(name="a1")))
                .only("id", "name")
                .first()
            )
            assert first.author is not None
            assert first.author.name == "a1"
            second = (
                await Book.objects.filter(name="alpha")
                .select_related(Select("author", extra_condition=Q(name="a2")))
                .only("id", "name")
                .first()
            )
            assert second.author is None
    assert spy.call_count > 0


@pytest.mark.asyncio
async def test_select_related_extra_condition_survives_order_by_same_relation(db):
    """Regression: ordering by the SAME relation carrying `extra_condition` used to build its own
    unconditioned JOIN in `get_ordering()`, which ran BEFORE `_join_select_related()` in
    `_make_query()` - so it won `_join_table()`'s table-identity dedup and silently dropped
    `extra_condition` from the real query, even on the first, uncached build.

    `get_ordering()` now folds the SAME extra_condition into its own (earlier) JOIN itself
    when it crosses a relation also named by `Select(relation, extra_condition=...)` - so the
    query is correct on every call. The JOIN records the condition apart from the query's own
    values, so the second call runs on the plan with its own condition."""
    await Author.objects.create(name="a1")
    author_a2 = await Author.objects.create(name="a2")
    await Book.objects.create(name="beta", author=author_a2, rating=1.0)

    with _spy_on_plan_hits() as spy:
        first = (
            await Book.objects.filter(name="beta")
            .select_related(Select("author", extra_condition=Q(name="a1")))
            .order_by("author__name")
            .first()
        )
        second = (
            await Book.objects.filter(name="beta")
            .select_related(Select("author", extra_condition=Q(name="a2")))
            .order_by("author__name")
            .first()
        )
    assert spy.call_count == 1
    assert first is not None
    assert first.author is None  # "a1" doesn't match the real author's name ("a2")
    assert second is not None
    assert second.author is not None
    assert second.author.name == "a2"


@pytest.mark.asyncio
async def test_select_related_extra_condition_survives_nested_filter_on_same_relation(db):
    """Same hazard as ordering (see test_select_related_extra_condition_survives_order_by_same_
    relation above), via a nested filter kwarg instead: `.filter(author__id=...)` on the SAME
    relation builds its own JOIN through `Q._get_nested_filter()` inside `get_filters()`,
    which must not win the dedup over the extra_condition-aware JOIN either.

    `Q._get_nested_filter()` now folds the SAME extra_condition into its own (earlier) JOIN
    itself when it crosses a relation also named by `Select(relation, extra_condition=...)` - so
    the query is correct on every call. The JOIN records the condition apart from the query's own
    values, so the second call runs on the plan with its own condition.

    Uses `author__id` (not `author__name`) for the nested filter, matching the real author's id in
    BOTH calls - once extra_condition is correctly folded into the JOIN's ON clause, a mismatching
    extra_condition (the second call's `name="a2"`) nulls out every column of that joined row
    (LEFT JOIN semantics), so the nested `author__id=...` filter in the WHERE clause can no longer
    match either - the whole row disappears, it doesn't just leave `.author` as `None`. That is
    the correct, live-confirmed distinguishing signal here: with the pre-fix bug (extra_condition
    silently dropped from the query entirely), the JOIN stays unconditioned, the nested filter
    still matches normally, and the row is wrongly still returned - which is exactly what this
    test would need to fail on to catch a regression."""
    author = await Author.objects.create(name="a1")
    book = await Book.objects.create(name="beta", author=author, rating=1.0)

    with _spy_on_plan_hits() as spy:
        first = (
            await Book.objects.filter(pk=book.pk, author__id=author.pk)
            .select_related(Select("author", extra_condition=Q(name="a1")))
            .first()
        )
        second = (
            await Book.objects.filter(pk=book.pk, author__id=author.pk)
            .select_related(Select("author", extra_condition=Q(name="a2")))
            .first()
        )
    assert spy.call_count == 1
    assert first is not None
    assert first.author is not None
    assert first.author.name == "a1"  # extra_condition="a1" matches the real author
    assert second is None  # extra_condition="a2" doesn't match, and the nested filter can't
    # match a NULL-ed-out author.id either - the row disappears entirely, not just .author


@pytest.mark.asyncio
async def test_only_fields_still_correct_and_can_use_fast_path(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = (await Book.objects.filter(name="alpha").only("id", "name").all())[0]
        second = (await Book.objects.filter(name="beta").only("id", "name").all())[0]
    assert spy.call_count > 0
    assert first.name == "alpha"
    assert second.name == "beta"


@pytest.mark.asyncio
async def test_values_and_values_list_not_broken_by_cache(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    # warm the plain .filter() shape first
    await Book.objects.filter(name="alpha").all()

    values_result = await Book.objects.filter(name="beta").values("name", "rating")
    values_list_result = await Book.objects.filter(name="beta").values_list("name", flat=True)
    assert values_result == [{"name": "beta", "rating": 2.0}]
    assert list(values_list_result) == ["beta"]


@pytest.mark.asyncio
async def test_function_aggregate_annotate_now_fast_pathed_and_stays_correct(db):
    """A bare `Count("books")` annotation (no `default_values`, no `_filter=Q(...)`) is now
    cacheable - `_annotation_is_cacheable()`'s `AnnotationFunction` branch accepts it since there's
    no literal embedded anywhere in it to substitute (the `hare.sql.functions` class each of
    `Sum`/`Avg`/`Count`/`Max`/`Min` wraps only ever takes the one main term)."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            result = (await Author.objects.annotate(book_count=Count("books")).filter(name="a").all())[0]
            assert result.book_count == 2
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_combined_expression_annotate_now_fast_pathed_and_stays_correct(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            results = await Book.objects.annotate(bumped=F("rating") * 1.5).filter(name="alpha").all()
            assert abs(results[0].bumped - 1.5) < 1e-9
    # A nonzero count is the signal the fast path actually activated (see
    # _spy_on_plan_hits()'s own docstring).
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()

    alpha = await Book.objects.annotate(bumped=F("rating") * 1.5).filter(name="alpha").all()
    beta = await Book.objects.annotate(bumped=F("rating") * 1.5).filter(name="beta").all()
    assert abs(alpha[0].bumped - 1.5) < 1e-9
    assert abs(beta[0].bumped - 3.0) < 1e-9
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_combined_expression_int_vs_float_literal_are_separate_cache_entries(db):
    """CombinedExpression._literal_term() wraps a float/Decimal literal operand in a Cast(...) -
    baked into the cached Term tree's own structure, not something the substitution mechanism can
    retrofit onto a hit - but leaves an int operand uncast. _annotation_shape_key() used to return
    the same bare "Value" placeholder for both, so an int-literal call followed by a float-literal
    call against the SAME annotation shape incorrectly reused the int-literal's uncast SQL
    structure (a live-verified structural divergence on Postgres/asyncpg, where an uncast literal
    resolves its bind-parameter type from the OTHER (int) operand and truncates the fraction)."""
    await IntFields.objects.create(intnum=7)

    StatementPlans.plans.clear()
    int_result = await IntFields.objects.annotate(result=F("intnum") + 1).values("result")
    assert int_result == [{"result": 8}]
    assert len(StatementPlans.plans) == 1

    float_result = await IntFields.objects.annotate(result=F("intnum") + 0.5).values("result")
    assert float_result == [{"result": 7.5}]
    assert len(StatementPlans.plans) == 2, "a float literal must not reuse an int literal's uncast SQL structure"


@pytest.mark.asyncio
async def test_annotate_f_cross_reference_runs_on_its_plan(db):
    """An annotation reading another one by name (`F("bumped")`) resolves it again - its values are
    counted, and bound, at both places."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    with _spy_on_plan_hits() as spy:
        for factor, multiplier in ((1.5, 2), (2.5, 3), (1.5, 4)):
            results = (
                await Book.objects.annotate(bumped=F("rating") * factor)
                .annotate(doubled=F("bumped") * multiplier)
                .filter(name="alpha")
                .all()
            )
            assert abs(results[0].bumped - factor) < 1e-9
            assert abs(results[0].doubled - factor * multiplier) < 1e-9
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_annotation_tree(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    first_query = Book.objects.annotate(bumped=F("rating") * 1.5).filter(name="alpha")
    await first_query.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_select_ids = {
        id(node) for select in cached_query._selects for node in select.nodes_() if isinstance(node, ValueWrapper)
    }
    cached_where_ids = _value_wrapper_ids(cached_query._wheres)
    cached_ids = cached_select_ids | cached_where_ids
    assert cached_ids, "expected at least one ValueWrapper leaf in the cached annotation template"

    second_query = Book.objects.annotate(bumped=F("rating") * 1.5).filter(name="beta")
    built_second_query = _built(second_query)
    fresh_query = built_second_query.query
    fresh_select_ids = {
        id(node) for select in fresh_query._selects for node in select.nodes_() if isinstance(node, ValueWrapper)
    }
    fresh_where_ids = _value_wrapper_ids(fresh_query._wheres)
    fresh_ids = fresh_select_ids | fresh_where_ids

    assert not (cached_ids & fresh_ids), "a cloned annotation query must never reuse the cached template's own nodes"


@pytest.mark.asyncio
async def test_dialect_is_part_of_the_cache_key(db):
    """Two otherwise-identical shapes under different dialects must occupy different cache
    entries - same principle EXECUTOR_CACHE already relies on (see its own key), verified here
    directly against the key/value hare.sql object rather than via a second live connection
    (this file runs against both sqlite and postgres in CI, so "the other dialect" is picked
    relative to whichever one this particular run is actually using, not hardcoded - hardcoding
    "postgres" here once silently made this a same-dialect no-op under a postgres test run)."""
    await Author.objects.create(name="a")
    # Both dialects registered up front - registering one drops every query-shape cache entry.
    DialectRegistry.get_dialect("postgresql")
    DialectRegistry.get_dialect("sqlite")
    qs = Author.objects.filter(name="a")._get_compiler()._get_execution_query()
    real_dialect = qs.dialect.name
    qs._make_query()
    real_key = next(iter(StatementPlans.plans))

    other_dialect = DialectRegistry.get_dialect("postgresql" if real_dialect != "postgresql" else "sqlite")
    qs2 = Author.objects.filter(name="a")._get_compiler()._get_execution_query()
    with patch.object(type(qs2), "dialect", other_dialect):
        qs2._make_query()

    assert len(StatementPlans.plans) == 2
    other_key = next(key for key in StatementPlans.plans if key != real_key)
    # The model, the query class, its declaration, the model again, then the dialect.
    assert real_key[4] != other_key[4]


@pytest.mark.asyncio
async def test_concurrent_calls_with_different_values_do_not_cross_talk(db):
    """asyncio, not threading - but still worth proving directly: many concurrent
    `.filter(name=<distinct value>).all()` calls for the same shape, interleaved by the
    event loop across real DB round-trips, must each get back their OWN row, never another
    coroutine's rebound value. Deliberately not `.first()`/`.get()` - those set `_limit` and
    never take the fast path at all (see test_limit_offset_and_select_for_update_excluded_
    from_fast_path), which would make this test pass trivially regardless of whether the new
    caching mechanism is actually safe under interleaving."""
    authors = [await Author.objects.create(name=f"author-{i}") for i in range(20)]

    async def fetch(expected_name: str) -> str:
        rows = await Author.objects.filter(name=expected_name).order_by("id")
        return rows[0].name

    with _spy_on_plan_hits() as spy:
        results = await asyncio.gather(*(fetch(a.name) for a in authors))
    assert results == [a.name for a in authors]
    assert spy.call_count > 0


@pytest.mark.asyncio
async def test_cache_survives_across_separate_queryset_instances(db):
    await Author.objects.create(name="a")
    await Author.objects.filter(name="a")

    with _spy_on_plan_hits() as spy:
        assert [author.name for author in await Author.objects.filter(name="a")] == ["a"]
    assert spy.call_count > 0


@pytest.mark.asyncio
async def test_after_cursor_now_fast_pathed_and_stays_correct(db):
    author = await Author.objects.create(name="a")
    for i in range(1, 6):
        await Book.objects.create(name=f"book{i}", author=author, rating=float(i))

    with _spy_on_plan_hits() as spy:
        page1 = await Book.objects.filter(author_id=author.id).order_by("rating").after_cursor(2.0).limit(2).all()
        assert [b.rating for b in page1] == [3.0, 4.0]
        page2 = await Book.objects.filter(author_id=author.id).order_by("rating").after_cursor(4.0).limit(2).all()
        assert [b.rating for b in page2] == [5.0]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_after_cursor_vs_no_cursor_same_ordering_are_distinct_cache_entries(db):
    author = await Author.objects.create(name="a")
    for i in range(1, 4):
        await Book.objects.create(name=f"book{i}", author=author, rating=float(i))

    no_cursor = await Book.objects.filter(author_id=author.id).order_by("rating").all()
    assert len(no_cursor) == 3
    with_cursor = await Book.objects.filter(author_id=author.id).order_by("rating").after_cursor(1.0).all()
    assert [b.rating for b in with_cursor] == [2.0, 3.0]
    assert len(StatementPlans.plans) == 2


@pytest.mark.asyncio
async def test_after_cursor_multi_field_mixed_ordering_now_fast_pathed(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="book1", author=author, rating=1.0)
    await Book.objects.create(name="book1b", author=author, rating=1.0)
    await Book.objects.create(name="book2", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        for _ in range(2):
            rows = (
                await Book.objects.filter(author_id=author.id)
                .order_by("rating", "-name")
                .after_cursor(1.0, "book1")
                .all()
            )
            names = [b.name for b in rows]
            assert "book1" not in names
            assert "book2" in names
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_after_cursor_null_boundary_keeps_a_plan_of_its_own(db):
    """A None boundary is an IS NULL/IS NOT NULL test with no value - which boundaries are None
    is part of the key, so it never runs on the plan of a boundary with a value, nor the other
    way round."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="book_with_subject", author=author, rating=1.0, subject="fiction")
    await Book.objects.create(name="book_without_subject", author=author, rating=2.0, subject=None)
    await Book.objects.create(name="book_with_other_subject", author=author, rating=3.0, subject="history")

    def after(subject):
        return Book.objects.filter(author_id=author.id).order_by("subject", "rating").after_cursor(subject, 0.0)

    async def names(queryset):
        return [book.name for book in await queryset]

    built = (await names(after(None)), await names(after("fiction")))
    # NULLs sort first on SQLite, last on PostgreSQL.
    assert built in (
        (
            ["book_without_subject", "book_with_subject", "book_with_other_subject"],
            ["book_with_subject", "book_with_other_subject"],
        ),
        (["book_without_subject"], ["book_with_subject", "book_with_other_subject", "book_without_subject"]),
    )
    hits = StatementPlans.hits
    assert (await names(after(None)), await names(after("fiction"))) == built
    assert StatementPlans.hits - hits == 2


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_cursor_tree(db):
    author = await Author.objects.create(name="a")
    for i in range(1, 6):
        await Book.objects.create(name=f"book{i}", author=author, rating=float(i))

    first_query = Book.objects.filter(author_id=author.id).order_by("rating").after_cursor(2.0)
    await first_query.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids, "expected at least one ValueWrapper leaf in the cached cursor template"

    second_query = Book.objects.filter(author_id=author.id).order_by("rating").after_cursor(4.0)
    built_second_query = _built(second_query)
    fresh_ids = _value_wrapper_ids(built_second_query.query._wheres)

    assert not (cached_ids & fresh_ids), "a cloned cursor query must never reuse the cached template's own nodes"

    results = await second_query.all()
    assert [b.rating for b in results] == [5.0]


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_with_cte_unrelated_to_outer_filter_now_fast_pathed(db):
    author = await Author.objects.create(name="a")
    for i in range(1, 4):
        await Book.objects.create(name=f"book{i}", author=author, rating=float(i))

    with _spy_on_plan_hits() as spy:
        rows1 = await Book.objects.filter(name="book2").with_cte("cte1", Book.objects.filter(rating__lt=2.0)).all()
        assert [b.name for b in rows1] == ["book2"]

        # The same CTE body shape with another value - the body's values are bound per query.
        rows2 = await Book.objects.filter(name="book3").with_cte("cte1", Book.objects.filter(rating__lt=3.5)).all()
        assert [b.name for b in rows2] == ["book3"]
    assert spy.call_count > 0

    # The cache HIT's attached CTE must reflect THIS call's own inner queryset, not a stale
    # first-call one - confirmed by inspecting the actual rendered SQL text.
    outer_qs = Book.objects.filter(name="book3").with_cte("cte1", Book.objects.filter(rating__gt=1.5))
    built_outer_qs = _built(outer_qs)
    sql = built_outer_qs.query.get_sql(built_outer_qs.query.query_class.SQL_CONTEXT)
    assert "rating" in sql and '"cte1"' in sql


@pytest.mark.asyncio
async def test_with_cte_raw_sql_reference_keeps_one_plan_with_its_cte(db):
    """The documented `.with_cte(...).filter(id__in=RawSQL(...))` pattern keeps one plan: the
    RawSQL text is part of the shape, and the CTE body is built into the plan with its values
    bound per query - no plan of its own."""
    author = await Author.objects.create(name="a")
    for i in range(1, 4):
        await Book.objects.create(name=f"book{i}", author=author, rating=float(i))

    for _ in range(2):
        cheap_books = Book.objects.filter(rating__lt=2.0)
        outer_qs = Book.objects.all().with_cte("cheap", cheap_books).filter(id__in=RawSQL('SELECT id FROM "cheap"'))
        rows = await outer_qs.all()
        assert [b.name for b in rows] == ["book1"]

    assert len(StatementPlans.plans) == 1
    plan = next(iter(StatementPlans.plans.values()))
    assert plan.sql is not None and '"cheap"' in plan.sql


def _value_wrapper_ids_including_exists(criterion: Criterion | None) -> set[int]:
    """`_value_wrapper_ids()` plus a NOT EXISTS-aware extension - `QueryBuilder.nodes_()` isn't
    overridden (falls back to the `Node` default, `yield self` only), so it never descends into
    its own `_wheres`/`_joins`; an `ExistsTerm`'s `.inner_query` needs its own explicit walk."""
    ids = _value_wrapper_ids(criterion)
    if isinstance(criterion, Not) and isinstance(criterion.term, ExistsTerm):
        ids |= _value_wrapper_ids_including_exists(criterion.term.inner_query._wheres)
    return ids


@pytest.mark.asyncio
async def test_plain_join_crossing_filter_now_fast_pathed_and_stays_correct(db):
    alice = await Author.objects.create(name="alice")
    bob = await Author.objects.create(name="bob")
    await Book.objects.create(name="alpha", author=alice, rating=1.0)
    await Book.objects.create(name="beta", author=bob, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(author__name="alice").all()
        second = await Book.objects.filter(author__name="bob").all()
    assert [b.name for b in first] == ["alpha"]
    assert [b.name for b in second] == ["beta"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_negated_join_crossing_filter_now_fast_pathed_and_stays_correct(db):
    alice = await Author.objects.create(name="alice")
    bob = await Author.objects.create(name="bob")
    await Book.objects.create(name="alpha", author=alice, rating=1.0)
    await Book.objects.create(name="beta", author=bob, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.exclude(author__name="alice").all()
        second = await Book.objects.exclude(author__name="bob").all()
    assert [b.name for b in first] == ["beta"]
    assert [b.name for b in second] == ["alpha"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_negated_join_crossing_filter_null_fk_row_stays_included(db):
    """The whole reason .exclude() across a relation rewrites to NOT EXISTS instead of a naive
    NOT(joined.col = value): a row with NO related object at all (FK is NULL) must still count as
    "excluded from matching X" - repeated, cached calls must never regress this (a stale/wrong
    substitution could plausibly reintroduce exactly this bug class)."""
    tournament = await Tournament.objects.create(name="Tournament")
    john = await Reporter.objects.create(name="John")
    event_with_reporter = await Event.objects.create(name="has reporter", tournament=tournament, reporter=john)
    event_without_reporter = await Event.objects.create(name="no reporter", tournament=tournament)

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            excluded = await Event.objects.exclude(reporter__name="John")
            assert {e.name for e in excluded} == {event_without_reporter.name}
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    excluded_other = await Event.objects.exclude(reporter__name="Nobody Matches This")
    assert {e.name for e in excluded_other} == {event_with_reporter.name, event_without_reporter.name}


@pytest.mark.asyncio
async def test_any_lookup_across_a_relation_runs_on_its_plan(db):
    """A filter across a relation keeps its plan whatever its lookup - each later query binds its
    own values, converted for the related model."""
    alice = await Author.objects.create(name="alice")
    bob = await Author.objects.create(name="bob")
    carol = await Author.objects.create(name="carol")
    await Book.objects.create(name="alpha", author=alice, rating=1.0)
    await Book.objects.create(name="beta", author=bob, rating=2.0)
    await Book.objects.create(name="gamma", author=carol, rating=3.0)

    shapes = [
        (lambda names: Book.objects.filter(author__name__in=names), (["alice", "bob"], ["carol", "bob"])),
        (lambda prefix: Book.objects.filter(author__name__startswith=prefix), ("a", "c")),
        (lambda bounds: Book.objects.filter(author__books__rating__range=bounds).distinct(), ((0.5, 1.5), (1.5, 3.5))),
        (lambda name: Book.objects.filter(author__name__gt=name), ("alice", "bob")),
        (lambda name: Book.objects.exclude(author__name=name), ("alice", "carol")),
        (lambda name: Author.objects.filter(books__author__name=name), ("bob", "carol")),
    ]
    for build, (first_value, second_value) in shapes:
        StatementPlans.plans.clear()
        await build(first_value)
        with full_build():
            expected = sorted(str(row.pk) for row in await build(second_value))
        with _spy_on_plan_hits() as spy:
            rows = await build(second_value)
        assert sorted(str(row.pk) for row in rows) == expected
        assert spy.call_count == 1


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_not_exists_tree(db):
    alice = await Author.objects.create(name="alice")
    bob = await Author.objects.create(name="bob")
    await Book.objects.create(name="alpha", author=alice, rating=1.0)
    await Book.objects.create(name="beta", author=bob, rating=2.0)

    first_query = Book.objects.exclude(author__name="alice")
    await first_query.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids_including_exists(cached_query._wheres)
    assert cached_ids, "expected at least one ValueWrapper leaf in the cached NOT EXISTS template"

    second_query = Book.objects.exclude(author__name="bob")
    built_second_query = _built(second_query)
    fresh_ids = _value_wrapper_ids_including_exists(built_second_query.query._wheres)

    assert not (cached_ids & fresh_ids), "a cloned NOT EXISTS query must never reuse the cached template's own nodes"

    results = await second_query.all()
    assert [b.name for b in results] == ["alpha"]


@pytest.mark.asyncio
async def test_like_family_lookups_now_fast_pathed_and_stay_correct(db):
    """Case-sensitive __contains/__startswith/__endswith all resolve through the SAME
    Like(BasicCriterion) shape (hare.query.filters.lookups.lookups.Lookups._get_like_pattern()) - one test covers
    all three, each with repeated calls at DIFFERENT values."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="the great gatsby", author=author, rating=1.0)
    await Book.objects.create(name="moby dick", author=author, rating=2.0)
    await Book.objects.create(name="dune", author=author, rating=3.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name__contains="great").all()
        second = await Book.objects.filter(name__contains="dick").all()
    assert [b.name for b in first] == ["the great gatsby"]
    assert [b.name for b in second] == ["moby dick"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name__startswith="the").all()
        second = await Book.objects.filter(name__startswith="dune").all()
    assert [b.name for b in first] == ["the great gatsby"]
    assert [b.name for b in second] == ["dune"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name__endswith="dick").all()
        second = await Book.objects.filter(name__endswith="dune").all()
    assert [b.name for b in first] == ["moby dick"]
    assert [b.name for b in second] == ["dune"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_like_family_lookup_escapes_literal_wildcard_characters(db):
    """A value containing a literal `%`/`_` must stay a literal match, not a wildcard - on both
    the write call AND every cache-hit substitution (like_pattern_text() re-escapes the FRESH
    value each time, never reuses the old escaped text)."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="100% pure", author=author, rating=1.0)
    await Book.objects.create(name="100X pure", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        percent_match = await Book.objects.filter(name__contains="100%").all()
        assert [b.name for b in percent_match] == ["100% pure"]
        underscore_match = await Book.objects.filter(name__contains="_").all()
        assert underscore_match == []
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_like_family_case_insensitive_variants_now_fast_pathed_and_stay_correct(db):
    """The pattern is embedded one level deeper than the case-sensitive variants (wrapped in an
    UPPER(...) SQL function call, not a bare ValueWrapper) - a plan binds it by the term it is
    rendered from, the same way as a Cast(...)-wrapped annotation literal."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="the GREAT gatsby", author=author, rating=1.0)
    await Book.objects.create(name="moby DICK", author=author, rating=2.0)

    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name__icontains="great").all()
        second = await Book.objects.filter(name__icontains="dick").all()
    assert [b.name for b in first] == ["the GREAT gatsby"]
    assert [b.name for b in second] == ["moby DICK"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name__istartswith="the").all()
        second = await Book.objects.filter(name__istartswith="moby").all()
    assert [b.name for b in first] == ["the GREAT gatsby"]
    assert [b.name for b in second] == ["moby DICK"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await Book.objects.filter(name__iendswith="gatsby").all()
        second = await Book.objects.filter(name__iendswith="dick").all()
    assert [b.name for b in first] == ["the GREAT gatsby"]
    assert [b.name for b in second] == ["moby DICK"]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_case_insensitive_like_tree(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="the great gatsby", author=author, rating=1.0)
    await Book.objects.create(name="moby dick", author=author, rating=2.0)

    first_query = Book.objects.filter(name__icontains="great")
    await first_query.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids, "expected at least one ValueWrapper leaf in the cached UPPER(...)-wrapped LIKE template"

    second_query = Book.objects.filter(name__icontains="dick")
    built_second_query = _built(second_query)
    fresh_ids = _value_wrapper_ids(built_second_query.query._wheres)

    assert not (cached_ids & fresh_ids), (
        "a cloned case-insensitive LIKE query must never reuse the cached template's own nodes"
    )

    results = await second_query.all()
    assert [b.name for b in results] == ["moby dick"]


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_like_tree(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="the great gatsby", author=author, rating=1.0)
    await Book.objects.create(name="moby dick", author=author, rating=2.0)

    first_query = Book.objects.filter(name__contains="great")
    await first_query.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids(cached_query._wheres)
    assert cached_ids, "expected at least one ValueWrapper leaf in the cached LIKE template"

    second_query = Book.objects.filter(name__contains="dick")
    built_second_query = _built(second_query)
    fresh_ids = _value_wrapper_ids(built_second_query.query._wheres)

    assert not (cached_ids & fresh_ids), "a cloned LIKE query must never reuse the cached template's own nodes"

    results = await second_query.all()
    assert [b.name for b in results] == ["moby dick"]


@pytest.mark.asyncio
async def test_json_contains_and_contained_by_already_fast_pathed_and_stay_correct(db):
    """Not new code - a live-verified finding: JSONField's `__contains`/`__contained_by`
    (`postgres_json_contains`/`postgres_json_contained_by`, hare/dialects/postgresql/lookups/json.py)
    register NO `value_encoder` at all (`get_json_filter()`), unlike every other value_encoder-
    based lookup this increment covers - the raw value goes through JSONField's own plain
    `to_db_value()` instead, landing in the ALREADY-existing `ScalarValueReference` path from Increment
    1 (a bare `BasicCriterion` with a `ValueWrapper` right-hand side). Pinned here as a permanent
    regression test since it was easy to assume otherwise from the plan's own text."""
    a = await JSONFields.objects.create(data={"a": 1, "b": 2})
    b = await JSONFields.objects.create(data={"c": 3})

    # On SQLite the lookup is a UDF call holding the value as its one parameter.
    with _spy_on_plan_hits() as spy:
        first = await JSONFields.objects.filter(data__contains={"a": 1}).all()
        second = await JSONFields.objects.filter(data__contains={"c": 3}).all()
    assert [obj.id for obj in first] == [a.id]
    assert [obj.id for obj in second] == [b.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await JSONFields.objects.filter(data__contained_by={"a": 1, "b": 2}).all()
        second = await JSONFields.objects.filter(data__contained_by={"c": 3}).all()
    assert [obj.id for obj in first] == [a.id]
    assert [obj.id for obj in second] == [b.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_contains_contained_by_overlap_now_fast_pathed_and_stay_correct(db_array_fields):
    """`ArrayField.__contains`/`__contained_by`/`__overlap` (`array_encoder` -> `Cast(Array(...),
    type)`) - unlike every other ref shape in this file, a cache hit substitutes the WHOLE
    `Array` node, not a value leaf inside it (see `ArrayValueReference`'s own docstring for why - Array
    holds its own list as ONE lazily-parameterized bind value, not a per-element `ValueWrapper`
    a normal `__in` list would)."""
    from tests.testmodels_postgres import ArrayFields

    a = await ArrayFields.objects.create(array=[1, 2, 3])
    b = await ArrayFields.objects.create(array=[4, 5, 6])

    with _spy_on_plan_hits() as spy:
        first = await ArrayFields.objects.filter(array__contains=[1, 2]).all()
        second = await ArrayFields.objects.filter(array__contains=[4, 5]).all()
    assert [o.id for o in first] == [a.id]
    assert [o.id for o in second] == [b.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await ArrayFields.objects.filter(array__contained_by=[1, 2, 3, 9]).all()
        second = await ArrayFields.objects.filter(array__contained_by=[4, 5, 6, 9]).all()
    assert [o.id for o in first] == [a.id]
    assert [o.id for o in second] == [b.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await ArrayFields.objects.filter(array__overlap=[2, 99]).all()
        second = await ArrayFields.objects.filter(array__overlap=[5, 99]).all()
    assert [o.id for o in first] == [a.id]
    assert [o.id for o in second] == [b.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_len_now_fast_pathed_and_stays_correct(db_array_fields):
    """`ArrayField.__len` (`int_encoder`, then a plain `Coalesce(array_length(...), 0).eq(value)`
    comparison) - an ordinary `EncodedValueReference` shape, not `ArrayValueReference`."""
    from tests.testmodels_postgres import ArrayFields

    a = await ArrayFields.objects.create(array=[1, 2, 3])
    b = await ArrayFields.objects.create(array=[1, 2])

    with _spy_on_plan_hits() as spy:
        first = await ArrayFields.objects.filter(array__len=3).all()
        second = await ArrayFields.objects.filter(array__len=2).all()
    assert [o.id for o in first] == [a.id]
    assert [o.id for o in second] == [b.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_json_filter_runs_on_its_plan_and_stays_correct(db):
    """The JSON `__filter` lookup compares one piece of its dict - its plan key holds the dict's
    keys and the types of its values, and a later dict of that shape builds the criterion again and
    binds its pieces (`FieldLookup.binds_by_rebuild`). Another path or operator keeps another plan."""
    a = await JSONFields.objects.create(data={"breed": "labrador", "age": 3})
    b = await JSONFields.objects.create(data={"breed": "poodle", "age": 7})

    with _spy_on_plan_hits() as spy:
        for breed, expected in (("labrador", [a.id]), ("poodle", [b.id]), ("collie", [])):
            rows = await JSONFields.objects.filter(data__filter={"breed": breed}).order_by("id")
            assert [o.id for o in rows] == expected
        for age, expected in ((5, [b.id]), (1, [a.id, b.id])):
            rows = await JSONFields.objects.filter(data__filter={"age__gte": age}).order_by("id")
            assert [o.id for o in rows] == expected
    # The second and third breed, the second age.
    assert spy.call_count == 3


@pytest.mark.asyncio
async def test_json_has_keys_run_on_their_plan_and_stay_correct(db):
    """`__has_keys`/`__has_any_keys` compare a list of keys - a plan per list length."""
    a = await JSONFields.objects.create(data={"breed": "labrador", "age": 3})
    b = await JSONFields.objects.create(data={"breed": "poodle", "owner": "ann"})

    with _spy_on_plan_hits() as spy:
        for keys, expected in ((["breed", "age"], [a.id]), (["breed", "owner"], [b.id]), (["age", "owner"], [])):
            rows = await JSONFields.objects.filter(data__has_keys=keys).order_by("id")
            assert [o.id for o in rows] == expected
        for keys, expected in ((["age"], [a.id]), (["owner"], [b.id])):
            rows = await JSONFields.objects.filter(data__has_any_keys=keys).order_by("id")
            assert [o.id for o in rows] == expected
    assert spy.call_count == 3


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_array_contains_tree(db_array_fields):
    from tests.testmodels_postgres import ArrayFields

    a = await ArrayFields.objects.create(array=[1, 2, 3])
    b = await ArrayFields.objects.create(array=[4, 5, 6])

    first_query = ArrayFields.objects.filter(array__contains=[1, 2])
    first_results = await first_query.all()
    assert [o.id for o in first_results] == [a.id]
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_array_id = id(cached_query._wheres.args[1].value_wrapper)

    second_query = ArrayFields.objects.filter(array__contains=[4, 5])
    built_second_query = _built(second_query)
    fresh_array_id = id(built_second_query.query._wheres.args[1].value_wrapper)

    assert cached_array_id != fresh_array_id, "the Array node must be a FRESH object on a cache hit, not reused"
    assert cached_query._wheres.args[1].value == [1, 2]
    assert built_second_query.query._wheres.args[1].value == [4, 5]

    results = await second_query.all()
    assert [o.id for o in results] == [b.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_array_len_tree(db_array_fields):
    """`array_length(field, 1)`'s own `1` argument and `Coalesce(..., 0)`'s own `0` default are
    STRUCTURAL constants, fixed for every call of this shape regardless of the `__len` value being
    filtered on - correctly, safely SHARED (unchanged) across a cache hit, unlike the actual
    compared value's own `ValueWrapper`, which must always be a fresh object. This test checks
    identity of the SPECIFIC compared-value node directly (`criterion.right`), not a blanket
    "no ValueWrapper anywhere in the tree is shared" sweep, which would incorrectly flag those
    harmless shared constants as a symmetry violation."""
    from tests.testmodels_postgres import ArrayFields

    a = await ArrayFields.objects.create(array=[1, 2, 3])
    b = await ArrayFields.objects.create(array=[1, 2])

    first_query = ArrayFields.objects.filter(array__len=3)
    first_results = await first_query.all()
    assert [o.id for o in first_results] == [a.id]
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_value_id = id(cached_query._wheres.right)

    second_query = ArrayFields.objects.filter(array__len=2)
    built_second_query = _built(second_query)
    fresh_value_id = id(built_second_query.query._wheres.right)

    assert cached_value_id != fresh_value_id, "the compared ValueWrapper must be a FRESH object on a cache hit"
    assert cached_query._wheres.right.value == 3
    assert built_second_query.query._wheres.right.value == 2

    results = await second_query.all()
    assert [o.id for o in results] == [b.id]


@pytest.mark.asyncio
async def test_safe_custom_register_lookup_now_fast_pathed_and_stays_correct(db):
    """A `register_lookup()`-registered custom filter whose operator embeds `encoded_value`
    unchanged (a plain equality, no transformation) is automatically eligible for the fast path
    through the SAME generic `ScalarValueReference` mechanism every built-in scalar lookup uses -
    no lookup-specific code needed, `register_lookup()` authors get this for free as long as
    their operator doesn't transform the value further."""
    await CharFields.objects.create(char="alpha")
    await CharFields.objects.create(char="beta")

    with _spy_on_plan_hits() as spy:
        first = await CharFields.objects.filter(char__safe_custom="alpha").all()
        second = await CharFields.objects.filter(char__safe_custom="beta").all()
    assert {c.char for c in first} == {"alpha"}
    assert {c.char for c in second} == {"beta"}
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_unsafe_custom_register_lookup_stays_excluded_from_fast_path(db):
    """A real, silent-wrong-result bug found live during this increment's implementation: a
    `register_lookup()`-registered custom filter whose own operator TRANSFORMS the value (here,
    reverses it) before comparing was WRONGLY treated as a plain `ScalarValueReference` before this
    fix - a cache hit substituted `field.to_db_value(new_raw_value)` in place of whatever the
    custom operator actually embedded (the REVERSED value), silently comparing against the
    UNTRANSFORMED value instead and returning the FIRST call's row on every subsequent DIFFERENT
    value. `ScalarValueReference`'s own `criterion.right.value is encoded_value` identity check (added
    as this bug's fix) correctly excludes this shape now - `encoded_value` is `to_db_value()`'s
    own direct output, but the criterion embeds `value[::-1]`, a DIFFERENT object."""
    a = await CharFields.objects.create(char="moo")
    b = await CharFields.objects.create(char="oom")

    with _spy_on_plan_hits() as spy:
        for _ in range(3):
            # "oom" reversed is "moo" - must always match the row literally spelled "moo".
            first = await CharFields.objects.filter(char__unsafe_custom="oom").all()
            assert [c.id for c in first] == [a.id]
            # "moo" reversed is "oom" - must always match the row literally spelled "oom", never
            # a stale substitution that silently drops the reversal and matches "moo" again.
            second = await CharFields.objects.filter(char__unsafe_custom="moo").all()
            assert [c.id for c in second] == [b.id]
    assert spy.call_count == 0


@pytest.mark.asyncio
async def test_transforming_custom_lookup_declaring_a_rebuild_runs_on_its_plan(db):
    """A custom filter transforming its value runs on its plan once it declares `binds_by_rebuild` -
    each later value builds the criterion again, the reversal included."""
    a = await CharFields.objects.create(char="moo")
    b = await CharFields.objects.create(char="oom")

    with _spy_on_plan_hits() as spy:
        for _ in range(2):
            assert [c.id for c in await CharFields.objects.filter(char__rebuilt_custom="oom")] == [a.id]
            assert [c.id for c in await CharFields.objects.filter(char__rebuilt_custom="moo")] == [b.id]
    assert spy.call_count == 3


@pytest.mark.asyncio
async def test_exists_query_is_a_cache_hit_on_second_call_and_stays_correct(db):
    """`.exists()` (`ExistsQuery`) shares the SAME generic `AwaitableQuery`-level machinery as
    `QuerySet.filter()` (moved there specifically so this could reuse it) - its own `_make_query()`
    does the same shape-key/substitution dance, keyed separately from `QuerySet`'s own entries via
    a `ExistsQuery` type marker as the key's first element."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").exists()
        assert spy.call_count == 0
        second = await Book.objects.filter(name="alpha").exists()
        assert spy.call_count > 0
        third = await Book.objects.filter(name="nonexistent").exists()
    assert first is True
    assert second is True
    assert third is False
    exists_entries = [key for key in StatementPlans.plans if key[0] is ExistsQuery]
    assert len(exists_entries) == 1


@pytest.mark.asyncio
async def test_count_query_is_a_cache_hit_on_second_call_and_stays_correct(db):
    """`.count()` (`CountQuery`) - same reasoning as `test_exists_query_is_a_cache_hit_on_second_
    call_and_stays_correct`, keyed separately via a `CountQuery` type marker. Uses `author_id=<pk>`
    (a plain scalar filter), not `author=<instance>` - the latter is the FK-relational-shortcut
    shape, excluded from this cache regardless of query type, same as `QuerySet.filter()`."""
    author = await Author.objects.create(name="a")
    other_author = await Author.objects.create(name="b")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=other_author, rating=3.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(author_id=author.pk).count()
        assert spy.call_count == 0
        second = await Book.objects.filter(author_id=author.pk).count()
        assert spy.call_count > 0
        third = await Book.objects.filter(author_id=other_author.pk).count()
    assert first == 2
    assert second == 2
    assert third == 1
    count_entries = [key for key in StatementPlans.plans if key[0] is CountQuery]
    assert len(count_entries) == 1


@pytest.mark.asyncio
async def test_count_query_group_by_shares_the_plain_cache_entry(db):
    """A `.group_by(...)` doesn't change the instances `await qs` returns, so `count()` ignores it
    and shares the plain `CountQuery` shape."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=1.0)
    await Book.objects.create(name="gamma", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.all().group_by("rating").count()
        second = await Book.objects.all().count()
    assert spy.call_count > 0
    assert first == 3
    assert second == 3
    count_entries = [key for key in StatementPlans.plans if key[0] is CountQuery]
    assert len(count_entries) == 1


@pytest.mark.asyncio
async def test_exists_and_count_query_share_annotate_literal_fast_path(db):
    """An `.annotate()` literal is bound the same way for `ExistsQuery`/`CountQuery` as for
    `QuerySet` - and the annotation's SELECT column, which neither query type keeps, has no say in
    their plan."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.annotate(doubled=F("rating") * 2).filter(name="alpha").exists()
        assert spy.call_count == 0
        second = await Book.objects.annotate(doubled=F("rating") * 2).filter(name="beta").exists()
    assert spy.call_count > 0
    assert first is True
    assert second is True

    StatementPlans.plans.clear()
    with _spy_on_plan_hits_of_any_type() as spy:
        first_count = await Book.objects.annotate(doubled=F("rating") * 2).filter(name="alpha").count()
        assert spy.call_count == 0
        second_count = await Book.objects.annotate(doubled=F("rating") * 2).filter(name="beta").count()
    assert spy.call_count > 0
    assert first_count == 1
    assert second_count == 1


@pytest.mark.asyncio
async def test_contains_query_runs_on_its_own_plan_and_stays_correct(db):
    """`.contains(obj)` is the queryset's EXISTS with the object's primary key compared after it - a
    plan of its own, keyed apart from the plain `.exists()`, binding each object's key."""
    author = await Author.objects.create(name="a")
    alpha = await Book.objects.create(name="alpha", author=author, rating=1.0)
    beta = await Book.objects.create(name="beta", author=author, rating=2.0)
    other_author = await Author.objects.create(name="b")
    delta = await Book.objects.create(name="delta", author=other_author, rating=-5.0)

    queryset = Book.objects.filter(rating__gt=0)
    with _spy_on_plan_hits_of_any_type() as spy:
        assert await queryset.contains(alpha) is True
        assert await queryset.contains(beta) is True
        assert await queryset.contains(delta) is False
        assert await queryset.exists() is True
    assert spy.call_count == 2
    contains_entries = [key for key in StatementPlans.plans if key[0] is ContainsQuery]
    assert len(contains_entries) == 1


# A run takes the compiled statement, whose query is the stored one itself, read-only; the
# clone this checks is the full build every other hit (.sql(), a subquery) takes.
@patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)
@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_exists_and_count_query_tree(db):
    """Symmetry-principle regression test for `ExistsQuery`/`CountQuery` - a cache hit must never
    leave the CACHED template's own `ValueWrapper` reachable from the freshly built query, same
    invariant `test_repeated_shape_is_a_cache_hit_on_second_call` and friends check for
    `QuerySet`."""
    await Book.objects.create(name="alpha", author=await Author.objects.create(name="a"), rating=1.0)
    await Book.objects.create(name="beta", author=await Author.objects.create(name="b"), rating=2.0)

    first_exists_query = Book.objects.filter(name="alpha").exists()
    first_exists_query = _built(first_exists_query)
    first_ids = _value_wrapper_ids(first_exists_query.query._wheres)

    second_exists_query = Book.objects.filter(name="beta").exists()
    second_exists_query = _built(second_exists_query)
    second_ids = _value_wrapper_ids(second_exists_query.query._wheres)

    assert first_ids and second_ids
    assert first_ids.isdisjoint(second_ids)

    StatementPlans.plans.clear()
    first_count_query = Book.objects.filter(name="alpha").count()
    first_count_query = _built(first_count_query)
    first_count_ids = _value_wrapper_ids(first_count_query.query._wheres)

    second_count_query = Book.objects.filter(name="beta").count()
    second_count_query = _built(second_count_query)
    second_count_ids = _value_wrapper_ids(second_count_query.query._wheres)

    assert first_count_ids and second_count_ids
    assert first_count_ids.isdisjoint(second_count_ids)


@pytest.mark.asyncio
async def test_count_and_exists_query_timezone_is_part_of_the_cache_key(db):
    """Real, live-caught bug: the FIRST implementation of `CountQuery`/`ExistsQuery` caching
    copied `QuerySet._make_query()`'s shape key WITHOUT its `Timezone.name()` component - a
    `field__year`/`__month`/etc lookup renders the CONFIGURED zone directly into the cached
    `Extract(..., zone_name=...)` criterion (not just a substitutable leaf VALUE), so a shape
    populated under one zone silently kept extracting in that zone forever for a later call under
    a DIFFERENT configured zone, on the exact same filter KEY. Caught by the full sqlite suite
    (`tests/fields/test_time.py`'s own zone-parametrized regression, which shares this same
    process-global cache) after the fix landed for `QuerySet` alone but not yet for these two."""
    from datetime import datetime

    from hare.time import Timezone

    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        berlin_obj = await DatetimeFields.objects.create(
            datetime=datetime(2024, 1, 2, 0, 30, tzinfo=Timezone.default())
        )
        berlin_exists = await DatetimeFields.objects.filter(datetime__day=1, id=berlin_obj.id).exists()
        berlin_count = await DatetimeFields.objects.filter(datetime__day=1, id=berlin_obj.id).count()
    assert berlin_exists is False
    assert berlin_count == 0

    with override_timezone(use_timezone=True, timezone="Asia/Kolkata"):
        kolkata_obj = await DatetimeFields.objects.create(
            datetime=datetime(2024, 1, 2, 0, 30, tzinfo=Timezone.default())
        )
        # Same filter KEY (datetime__day=1) as the Berlin call above, under a DIFFERENT
        # configured zone - must never reuse Berlin's cached zone_name in the Extract(...) SQL.
        kolkata_exists = await DatetimeFields.objects.filter(datetime__day=1, id=kolkata_obj.id).exists()
        kolkata_count = await DatetimeFields.objects.filter(datetime__day=1, id=kolkata_obj.id).count()
    assert kolkata_exists is False
    assert kolkata_count == 0


@pytest.mark.asyncio
async def test_values_query_is_a_cache_hit_on_second_call_and_stays_correct(db):
    """`.values()` (`ValuesQuery`) shares the same generic `AwaitableQuery` machinery as
    `QuerySet.filter()`/`.exists()`/`.count()` - its own `_make_query()` does the same shape-key/
    substitution dance, keyed separately via a `ValuesQuery` type marker."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").values("name", "rating")
        assert spy.call_count == 0
        second = await Book.objects.filter(name="alpha").values("name", "rating")
        assert spy.call_count > 0
        third = await Book.objects.filter(name="beta").values("name", "rating")
    assert first == [{"name": "alpha", "rating": 1.0}]
    assert second == [{"name": "alpha", "rating": 1.0}]
    assert third == [{"name": "beta", "rating": 2.0}]
    values_entries = [key for key in StatementPlans.plans if key[0] is ValuesQuery]
    assert len(values_entries) == 1


@pytest.mark.asyncio
async def test_values_query_renamed_field_stays_correct_across_cache_hits(db):
    """`.values(renamed="rating")` - `return_as != field` - exercises
    `add_field_to_select_query()`'s own re-registration of the field under its return_as alias.
    Real, live-caught bug during this increment's implementation: for a field that's itself an
    ANNOTATION, that re-registration mutates `self._annotations` in a way that must be reflected
    consistently in both the cache key/precheck AND the write-gate leaf-count check (computing one
    before the mutation and the other after it permanently mismatched their lengths, silently
    falling back to a full rebuild on every call, safe but never actually cached) - this covers
    the plain-field rename shape, `test_values_and_values_list_query_annotate_literal_fast_path`
    below covers the annotation-field rename shape that actually caught the bug."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").values(renamed="rating")
        assert spy.call_count == 0
        second = await Book.objects.filter(name="beta").values(renamed="rating")
    assert spy.call_count > 0
    assert first == [{"renamed": 1.0}]
    assert second == [{"renamed": 2.0}]


@pytest.mark.asyncio
async def test_values_list_query_is_a_cache_hit_on_second_call_and_stays_correct(db):
    """`.values_list()` (`ValuesListQuery`) - same reasoning as the `ValuesQuery` test above,
    keyed separately via a `ValuesListQuery` type marker plus its own `_fields_for_select_list`/
    `_flat` shape components (a different requested-field ORDER or `flat=True` must never collide
    with a differently-shaped `.values_list()` call)."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").values_list("name", "rating")
        assert spy.call_count == 0
        second = await Book.objects.filter(name="alpha").values_list("name", "rating")
        assert spy.call_count > 0
        third = await Book.objects.filter(name="beta").values_list("name", "rating")
    assert first == [("alpha", 1.0)]
    assert second == [("alpha", 1.0)]
    assert third == [("beta", 2.0)]
    values_list_entries = [key for key in StatementPlans.plans if key[0] is ValuesQuery]
    assert len(values_list_entries) == 1


@pytest.mark.asyncio
async def test_values_list_query_flat_is_part_of_the_cache_key(db):
    """`flat=True` changes `_execute()`'s own return shape (`list[Any]` instead of
    `list[tuple[Any, ...]]`) for the exact same requested field - must never collide with the
    non-flat shape's own cache entry."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").values_list("rating", flat=True)
        second = await Book.objects.filter(name="beta").values_list("rating", flat=True)
    assert spy.call_count > 0
    assert first == [1.0]
    assert second == [2.0]


@pytest.mark.asyncio
async def test_values_and_values_list_query_annotate_literal_fast_path(db):
    """The `.annotate()` literal substitution mechanism is exercised the same way for
    `ValuesQuery`/`ValuesListQuery` as for `QuerySet`/`CountQuery`/`ExistsQuery`. Requesting the
    annotation BY NAME (`.values("name", "doubled")`) is also the shape that caught the real
    mutation-ordering bug documented on `ValuesQuery._make_query()` - `doubled` is both an
    annotation key AND the requested field name, so `add_field_to_select_query()`'s return_as
    re-registration duplicates its entry in `self._annotations`."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.annotate(doubled=F("rating") * 2).filter(name="alpha").values("name", "doubled")
        assert spy.call_count == 0
        second = await Book.objects.annotate(doubled=F("rating") * 2).filter(name="beta").values("name", "doubled")
    assert spy.call_count > 0
    assert first == [{"name": "alpha", "doubled": 2.0}]
    assert second == [{"name": "beta", "doubled": 4.0}]

    StatementPlans.plans.clear()
    with _spy_on_plan_hits_of_any_type() as spy:
        first_list = (
            await Book.objects.annotate(doubled=F("rating") * 2).filter(name="alpha").values_list("name", "doubled")
        )
        assert spy.call_count == 0
        second_list = (
            await Book.objects.annotate(doubled=F("rating") * 2).filter(name="beta").values_list("name", "doubled")
        )
    assert spy.call_count > 0
    assert first_list == [("alpha", 2.0)]
    assert second_list == [("beta", 4.0)]


@pytest.mark.asyncio
async def test_values_query_select_related_extra_condition_binds_each_calls_condition(db):
    """`add_field_to_select_query()`'s own forwarded-fields path (`"left__left__name"`) and
    `_get_group_bys()` both fold `self._select_related_extra_conditions.get(path)` into the JOIN of
    any relation crossed. A second `.values()` call with a DIFFERENT `extra_condition` value on the
    exact same requested fields/filter shape must not reuse the FIRST call's condition: the JOIN
    records the condition apart from the query's own values, so the second call runs on the plan
    with its own."""
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)

    rows_match = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="leaf")))
        .values("name", "left__left__name")
    )
    assert rows_match == [{"name": "root", "left__left__name": "leaf"}]

    rows_no_match = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="not-leaf")))
        .values("name", "left__left__name")
    )
    assert rows_no_match == [{"name": "root", "left__left__name": None}]
    extra_condition_entries = [key for key in StatementPlans.plans if key[0] is ValuesQuery]
    assert len(extra_condition_entries) == 1


# A run takes the compiled statement, whose query is the stored one itself, read-only; the
# clone this checks is the full build every other hit (.sql(), a subquery) takes.
@patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)
@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_values_query_tree(db):
    """Symmetry-principle regression test for `ValuesQuery`/`ValuesListQuery` - a cache hit must
    never leave the CACHED template's own `ValueWrapper` reachable from the freshly built query,
    same invariant every other query type's own version of this test checks."""
    await Book.objects.create(name="alpha", author=await Author.objects.create(name="a"), rating=1.0)
    await Book.objects.create(name="beta", author=await Author.objects.create(name="b"), rating=2.0)

    first_values_query = Book.objects.filter(name="alpha").values("name")
    first_values_query = _built(first_values_query)
    first_ids = _value_wrapper_ids(first_values_query.query._wheres)

    second_values_query = Book.objects.filter(name="beta").values("name")
    second_values_query = _built(second_values_query)
    second_ids = _value_wrapper_ids(second_values_query.query._wheres)

    assert first_ids and second_ids
    assert first_ids.isdisjoint(second_ids)

    StatementPlans.plans.clear()
    first_list_query = Book.objects.filter(name="alpha").values_list("name")
    first_list_query = _built(first_list_query)
    first_list_ids = _value_wrapper_ids(first_list_query.query._wheres)

    second_list_query = Book.objects.filter(name="beta").values_list("name")
    second_list_query = _built(second_list_query)
    second_list_ids = _value_wrapper_ids(second_list_query.query._wheres)

    assert first_list_ids and second_list_ids
    assert first_list_ids.isdisjoint(second_list_ids)


@pytest.mark.asyncio
async def test_aggregate_query_literal_metric_is_a_cache_hit_and_stays_correct(db):
    """`.aggregate()` (`AggregateQuery`) shares the same generic `AwaitableQuery` machinery as
    every other query type this cache covers - keyed separately via an `AggregateQuery` type
    marker plus `self._metric_keys` (the requested metric names). A purely computed
    (non-aggregate) metric is one cacheable shape; a real `Sum`/`Avg`/`Count`/`Max`/`Min` metric is
    now cacheable too - see `test_aggregate_query_real_aggregate_function_now_fast_pathed_and_
    stays_correct` below."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").aggregate(doubled=F("rating") * 2)
        assert spy.call_count == 0
        second = await Book.objects.filter(name="alpha").aggregate(doubled=F("rating") * 2)
        assert spy.call_count > 0
        third = await Book.objects.filter(name="beta").aggregate(doubled=F("rating") * 2)
    assert first == {"doubled": 2.0}
    assert second == {"doubled": 2.0}
    assert third == {"doubled": 4.0}
    aggregate_entries = [key for key in StatementPlans.plans if key[0] is AggregateQuery]
    assert len(aggregate_entries) == 1


@pytest.mark.asyncio
async def test_aggregate_query_real_aggregate_function_now_fast_pathed_and_stays_correct(db):
    """A real `.aggregate()` metric (`Sum`/`Avg`/`Count`/`Max`/`Min`) is now cacheable - none of
    these ever carry `default_values` (the `hare.sql.functions` class each wraps only accepts the
    one main term, nothing extra), so there's no embedded literal this fast path would need to
    find/substitute - `_annotation_is_cacheable()`'s `AnnotationFunction` branch accepts them."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(name="alpha").aggregate(total=Sum("rating"))
        assert spy.call_count == 0
        second = await Book.objects.filter(name="alpha").aggregate(total=Sum("rating"))
        assert spy.call_count > 0
        third = await Book.objects.filter(name="beta").aggregate(total=Sum("rating"))
    assert first == {"total": 1.0}
    assert second == {"total": 1.0}
    assert third == {"total": 2.0}
    aggregate_entries = [key for key in StatementPlans.plans if key[0] is AggregateQuery]
    assert len(aggregate_entries) == 1


@pytest.mark.asyncio
async def test_multiple_bare_aggregate_functions_together_now_fast_pathed(db):
    """Several bare aggregate metrics in one `.aggregate()` call, each recorded independently -
    the SAME shape/substitution machinery `_annotations_in_key_order()` already threads through
    for `CombinedExpression`/`Value` annotations covers multiple `AnnotationFunction` entries too,
    no special-casing needed for "more than one"."""
    author = await Author.objects.create(name="a")
    other_author = await Author.objects.create(name="b")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=other_author, rating=3.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(author_id=author.pk).aggregate(
            total=Sum("rating"), avg=Avg("rating"), cnt=Count("rating"), mx=Max("rating"), mn=Min("rating")
        )
        assert spy.call_count == 0
        second = await Book.objects.filter(author_id=other_author.pk).aggregate(
            total=Sum("rating"), avg=Avg("rating"), cnt=Count("rating"), mx=Max("rating"), mn=Min("rating")
        )
    assert spy.call_count > 0
    assert first == {"total": 3.0, "avg": 1.5, "cnt": 2, "mx": 2.0, "mn": 1.0}
    assert second == {"total": 3.0, "avg": 3.0, "cnt": 1, "mx": 3.0, "mn": 3.0}


@pytest.mark.asyncio
async def test_coalesce_literal_default_now_fast_pathed_and_stays_correct(db):
    """`Coalesce("field", 0)` carries a non-empty `default_values` - the literal `0`'s own
    `ValueWrapper` is built two layers down inside `hare.sql.terms.Function.__init__`'s own
    `wrap_constant()` call, NOT through `Value.get_result()` (the mechanism this cache's recording is
    normally built around) - now cacheable, `Function.get_result()` (`hare/query/expressions/
    function.py`) records a `LiteralValueReference` for it directly after the wrapped term is built."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.annotate(c=Coalesce("rating", 0)).filter(name="alpha").values("c")
        assert spy.call_count == 0
        second = await Book.objects.annotate(c=Coalesce("rating", 0)).filter(name="beta").values("c")
    assert spy.call_count > 0
    assert first == [{"c": 1.0}]
    assert second == [{"c": 2.0}]


@pytest.mark.asyncio
async def test_coalesce_decimal_default_stays_fast_pathed_and_substitutes_each_value(db):
    """A Decimal default next to a DecimalField is cast to NUMERIC on SQLite (so it compares as
    a number, not TEXT) - the cast wrapper must still expose the literal to the cache: a hit
    replaces the value instead of reusing the first call's."""
    await DecimalFields.objects.create(decimal=Decimal("1"), decimal_nodec=0, decimal_null=None)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await DecimalFields.objects.annotate(c=Coalesce("decimal_null", Decimal("2.50"))).values("c")
        assert spy.call_count == 0
        second = await DecimalFields.objects.annotate(c=Coalesce("decimal_null", Decimal("7.25"))).values("c")
    assert spy.call_count > 0
    assert first == [{"c": Decimal("2.5000")}]
    assert second == [{"c": Decimal("7.2500")}]


@pytest.mark.asyncio
async def test_case_decimal_literal_stays_fast_pathed_and_substitutes_each_value(db):
    await DecimalFields.objects.create(decimal=Decimal("1"), decimal_nodec=0)

    def build(default):
        return DecimalFields.objects.annotate(c=Case(When(decimal__gt=5, then=F("decimal")), default=Value(default)))

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await build(Decimal("2.50")).values_list("c", flat=True)
        assert spy.call_count == 0
        second = await build(Decimal("7.25")).values_list("c", flat=True)
    assert spy.call_count > 0
    assert [Decimal(str(value)) for value in first] == [Decimal("2.5")]
    assert [Decimal(str(value)) for value in second] == [Decimal("7.25")]


@pytest.mark.asyncio
async def test_coalesce_literal_default_type_is_part_of_the_shape_key(db):
    """`Coalesce` infers its output field from, and wraps an incompatible numeric literal in a
    Cast based on, its bare literal default's TYPE - so a compatible int default and an
    incompatible Decimal default sharing one annotation name must never collide into one cached
    shape (the second call would otherwise silently reuse the first's IntField decoding on a
    hit)."""
    await IntFields.objects.create(intnum=1, intnum_null=None)

    first = await IntFields.objects.annotate(c=Coalesce("intnum_null", 7)).values("c")
    second = await IntFields.objects.annotate(c=Coalesce("intnum_null", Decimal("5.55"))).values("c")
    third = await IntFields.objects.annotate(c=Coalesce("intnum_null", 7)).values("c")

    assert first == [{"c": 7}]
    assert Decimal(str(second[0]["c"])) == Decimal("5.55")
    assert third == [{"c": 7}]


@pytest.mark.asyncio
async def test_coalesce_value_default_type_is_part_of_the_shape_key(db):
    """Same as above for a default wrapped in `Value(...)` - Value(1) and Value(Decimal("5.55"))
    sharing one annotation name must never share one cached output field/term structure, and a
    repeated same-type Value default must still substitute its own value on a hit."""
    await IntFields.objects.create(intnum=1, intnum_null=None)

    first = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value(7))).values("c")
    second = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value(Decimal("5.55")))).values("c")
    third = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value(7))).values("c")
    fourth = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value(Decimal("6.66")))).values("c")
    fifth = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value(8))).values("c")

    assert first == [{"c": 7}]
    assert Decimal(str(second[0]["c"])) == Decimal("5.55")
    assert third == [{"c": 7}]
    assert Decimal(str(fourth[0]["c"])) == Decimal("6.66")
    assert fifth == [{"c": 8}]


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_coalesce_value_default_literal_type_beyond_cast_types_is_part_of_the_shape_key(db):
    """`Value.get_literal_output_field()` alone only tells float/Decimal apart from every other type - a text
    literal (compatible with nothing numeric, so no output field) must still not reuse the cached
    IntField output field of an earlier int `Value(...)` default. SQLite only: Postgres rejects
    COALESCE(integer, text) outright."""
    await IntFields.objects.create(intnum=1, intnum_null=None)

    first = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value(7))).values("c")
    second = await IntFields.objects.annotate(c=Coalesce("intnum_null", Value("abc"))).values("c")

    assert first == [{"c": 7}]
    assert second == [{"c": "abc"}]


@pytest.mark.asyncio
async def test_coalesce_default_value_is_substituted_correctly(db):
    """A DIFFERENT value passed as `Coalesce(...)`'s own default (via a Python variable, not a
    literal written differently in the source) must substitute correctly on a cache hit, not
    silently reuse the FIRST call's default."""
    author = await Author.objects.create(name="a")
    book = await Book.objects.create(name="alpha", author=author, rating=1.0, subject=None)

    def build(default):
        return Book.objects.annotate(c=Coalesce("subject", default)).filter(pk=book.pk)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await build("missing").values("c")
        assert spy.call_count == 0
        second = await build("N/A").values("c")
    assert spy.call_count > 0
    assert first == [{"c": "missing"}]
    assert second == [{"c": "N/A"}]


@pytest.mark.asyncio
async def test_aggregate_distinct_flag_is_part_of_the_cache_key(db):
    """`Count("rating", distinct=True)` and `Count("rating", distinct=False)` on the SAME filter
    shape must never collide into one cache entry - `.distinct` is a pure DDL-like flag (fixed at
    the annotation's own construction, never a runtime value), folded into
    `_annotation_shape_key()`'s own `AnnotationFunction` branch."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=1.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        # Different shapes (distinct True vs False) - both are cache MISSES, each populating its
        # own separate entry, never a hit on the other's template.
        distinct_count = await Book.objects.filter(author_id=author.pk).aggregate(cnt=Count("rating", distinct=True))
        plain_count = await Book.objects.filter(author_id=author.pk).aggregate(cnt=Count("rating", distinct=False))
    assert spy.call_count == 0
    assert distinct_count == {"cnt": 1}
    assert plain_count == {"cnt": 2}
    aggregate_entries = [key for key in StatementPlans.plans if key[0] is AggregateQuery]
    assert len(aggregate_entries) == 2

    # Each shape independently hits on a repeat call.
    with _spy_on_plan_hits_of_any_type() as spy:
        repeat_distinct = await Book.objects.filter(author_id=author.pk).aggregate(cnt=Count("rating", distinct=True))
    assert spy.call_count > 0
    assert repeat_distinct == {"cnt": 1}


@pytest.mark.asyncio
async def test_aggregate_filter_kwarg_now_fast_pathed_and_stays_correct(db):
    """`Sum("rating", _filter=Q(...))` embeds a SEPARATE runtime-value surface (a whole `Q` tree)
    inside the aggregate's own `FILTER (WHERE ...)` clause (rendered via `Aggregate._wrap_
    argument()`'s `Case(When(filter, then=field), default=None)` wrapping) - now cacheable:
    `self.filter.resolve(expression_context)` already threads through the SAME `Q.get_result()`
    machinery/`value_wrapper_references` any plain `.filter()` kwarg uses (same `expression_context`), and
    the resulting `Case` term is handled by the SAME generic `SqlCase` clone branch the `Case`/
    `When` annotation commit already added - only the structural precheck/leaf-tracking was
    missing."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=9.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.filter(author_id=author.pk).aggregate(total=Sum("rating", _filter=Q(rating__gte=8)))
        assert spy.call_count == 0
        second = await Book.objects.filter(author_id=author.pk).aggregate(
            total=Sum("rating", _filter=Q(rating__gte=8))
        )
    assert spy.call_count > 0
    assert first == {"total": 9.0}
    assert second == {"total": 9.0}
    aggregate_entries = [key for key in StatementPlans.plans if key[0] is AggregateQuery]
    assert len(aggregate_entries) == 1


@pytest.mark.asyncio
async def test_aggregate_filter_threshold_is_substituted_correctly(db):
    """A DIFFERENT literal embedded inside `Aggregate(..., _filter=Q(...))`'s own condition (not
    just the outer `.filter()`) must be substituted correctly on a cache hit, not silently reuse
    the FIRST call's threshold - the primary silent-wrong-result risk this whole cache is built
    around, same class of test as `test_case_when_condition_value_is_substituted_correctly`."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=9.0)
    await Book.objects.create(name="gamma", author=author, rating=15.0)

    def build(threshold):
        return Book.objects.filter(author_id=author.pk).aggregate(
            high_count=Count("rating", _filter=Q(rating__gte=threshold))
        )

    with _spy_on_plan_hits_of_any_type() as spy:
        low_threshold = await build(2)
        assert spy.call_count == 0
        high_threshold = await build(20)
    assert spy.call_count > 0
    assert low_threshold == {"high_count": 2}
    assert high_threshold == {"high_count": 0}


@pytest.mark.asyncio
async def test_window_row_number_now_fast_pathed_and_stays_correct(db):
    """`Window(RowNumber(), order_by=[...])` - zero args, nothing to embed at all - is now
    cacheable. WHERE applies BEFORE a window function in SQL's own logical processing order, so
    this is checked over the WHOLE table (filtering to one row would always give row_number=1,
    not a meaningful repeated-different-values check)."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(rn=Window(RowNumber(), order_by=["rating"]))
            .order_by("rating")
            .values("name", "rn")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(rn=Window(RowNumber(), order_by=["rating"]))
            .order_by("rating")
            .values("name", "rn")
        )
    assert spy.call_count > 0
    expected = [{"name": "alpha", "rn": 1}, {"name": "beta", "rn": 2}, {"name": "gamma", "rn": 3}]
    assert first == expected
    assert second == expected


@pytest.mark.asyncio
async def test_window_field_function_now_fast_pathed_and_stays_correct(db):
    """`Window(Sum("field"), order_by=[...])` (a `FieldWindowFunction`) - the one field arg is
    always resolved to a structural `Field`/`Term` reference, never a literal - is now cacheable."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(running=Window(WindowSum("rating"), order_by=["rating"]))
            .order_by("rating")
            .values("name", "running")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(running=Window(WindowSum("rating"), order_by=["rating"]))
            .order_by("rating")
            .values("name", "running")
        )
    assert spy.call_count > 0
    expected = [
        {"name": "alpha", "running": 1.0},
        {"name": "beta", "running": 3.0},
        {"name": "gamma", "running": 6.0},
    ]
    assert first == expected
    assert second == expected


@pytest.mark.asyncio
async def test_window_partition_by_is_part_of_the_cache_key(db):
    """A different `partition_by` on the same window function/order_by must never collide into
    one cache entry - `.partition_by` is purely structural (field names), folded into
    `_annotation_shape_key()`'s own `Window` branch."""
    author = await Author.objects.create(name="a")
    other_author = await Author.objects.create(name="b")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=other_author, rating=5.0)

    StatementPlans.plans.clear()
    await (
        Book.objects.annotate(rn=Window(RowNumber(), partition_by=["author_id"], order_by=["rating"]))
        .order_by("id")
        .values("rn")
    )
    with_partition_entries = len(StatementPlans.plans)

    await Book.objects.annotate(rn=Window(RowNumber(), order_by=["rating"])).order_by("id").values("rn")
    without_partition_entries = len(StatementPlans.plans)

    assert without_partition_entries == with_partition_entries + 1


@pytest.mark.asyncio
async def test_window_ntile_now_fast_pathed_and_stays_correct(db):
    """`NTile(buckets)` embeds `buckets` (a literal int) as its OWN main SQL arg - wrapped via the
    exact same `hare.sql.terms.Function.__init__`'s `wrap_constant()` path as a `Function`'s own
    `default_values` - now cacheable, `NTile.build()` (`hare/query/functions/window.py`) records a
    `LiteralValueReference` for it directly, mirroring `Coalesce`'s own default-value handling."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(bucket=Window(NTile(2), order_by=["rating"]))
            .order_by("rating")
            .values("bucket")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(bucket=Window(NTile(2), order_by=["rating"]))
            .order_by("rating")
            .values("bucket")
        )
    assert spy.call_count > 0
    assert first == [{"bucket": 1}, {"bucket": 2}]
    assert second == [{"bucket": 1}, {"bucket": 2}]
    ntile_entries = [key for key in StatementPlans.plans if key[0] is ValuesQuery]
    assert len(ntile_entries) == 1


@pytest.mark.asyncio
async def test_window_ntile_buckets_is_substituted_correctly(db):
    """A DIFFERENT `buckets` value (via a Python variable) must substitute correctly on a cache
    hit, not silently reuse the FIRST call's bucket count."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    def build(buckets):
        return Book.objects.annotate(bucket=Window(NTile(buckets), order_by=["rating"])).order_by("rating")

    with _spy_on_plan_hits_of_any_type() as spy:
        two_buckets = await build(2).values("bucket")
        assert spy.call_count == 0
        three_buckets = await build(3).values("bucket")
    assert spy.call_count > 0
    assert two_buckets == [{"bucket": 1}, {"bucket": 1}, {"bucket": 2}]
    assert three_buckets == [{"bucket": 1}, {"bucket": 2}, {"bucket": 3}]


@pytest.mark.asyncio
async def test_window_lag_with_explicit_default_now_fast_pathed_and_stays_correct(db):
    """`Lag(field, offset, default=<non-None literal>)` is now cacheable - both `offset` and
    `default` are recorded via `OffsetWindowFunction.build()`. `default=None` (implicit or
    explicit - indistinguishable once inside `__init__`) stays excluded, see
    `test_window_lag_default_none_stays_excluded_from_fast_path` below."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(prev=Window(Lag("rating", 1, default=0.0), order_by=["rating"]))
            .order_by("rating")
            .values("name", "prev")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(prev=Window(Lag("rating", 1, default=0.0), order_by=["rating"]))
            .order_by("rating")
            .values("name", "prev")
        )
    assert spy.call_count > 0
    expected = [{"name": "alpha", "prev": 0.0}, {"name": "beta", "prev": 1.0}]
    assert first == expected
    assert second == expected


@pytest.mark.asyncio
async def test_window_lag_default_none_is_fast_pathed_apart_from_a_real_default(db):
    """`Lag(field, offset)` - the most common shape, relying on `default`'s own Python default of
    `None` - keeps a plan: the `None` renders `LAG(..., NULL)` with no parameter, and is part of
    the shape, so a call whose `default` holds a real value (`LAG(..., ?)`) has a plan of its own."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    def previous(default=None):
        return (
            Book.objects.annotate(prev=Window(Lag("rating", 1, default), order_by=["rating"]))
            .order_by("rating")
            .values("prev")
        )

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await previous()
        assert spy.call_count == 0
        assert await previous() == first
        assert spy.call_count == 1
        with_default = await previous(0.5)
        assert spy.call_count == 1
    assert first == [{"prev": None}, {"prev": 1.0}]
    assert with_default == [{"prev": 0.5}, {"prev": 1.0}]


@pytest.mark.asyncio
async def test_case_when_literal_then_default_now_fast_pathed_and_stays_correct(db):
    """`Case(When(...), default=...)` with bare-literal `then=`/`default=` values is now
    cacheable - `case.py`'s `When.get_result()`/`Case.get_result()` now record a `LiteralValueReference` for
    each (mirroring `Value.get_result()`), and `When`'s own condition `Q` objects were ALREADY
    threaded through `value_wrapper_references` for free (they resolve through the exact same
    `Q.get_result()` machinery `.filter()`'s own `self._q_objects` use)."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=9.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(category=Case(When(rating__gte=8, then="big"), default="small"))
            .filter(name="alpha")
            .values("name", "category")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(category=Case(When(rating__gte=8, then="big"), default="small"))
            .filter(name="beta")
            .values("name", "category")
        )
    assert spy.call_count > 0
    assert first == [{"name": "alpha", "category": "small"}]
    assert second == [{"name": "beta", "category": "big"}]


@pytest.mark.asyncio
async def test_case_when_condition_value_is_substituted_correctly(db):
    """A DIFFERENT literal embedded inside `When(...)`'s own condition (not just the outer
    `.filter()`) must be substituted correctly on a cache hit, not silently reuse the FIRST
    call's threshold - the primary silent-wrong-result risk this whole cache is built around."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=9.0)

    def build(threshold):
        return Book.objects.annotate(category=Case(When(rating__gte=threshold, then="big"), default="small")).filter(
            name="alpha"
        )

    with _spy_on_plan_hits_of_any_type() as spy:
        low_threshold = await build(5).values("category")
        assert spy.call_count == 0
        high_threshold = await build(20).values("category")
    assert spy.call_count > 0
    assert low_threshold == [{"category": "big"}]
    assert high_threshold == [{"category": "small"}]


@pytest.mark.asyncio
async def test_case_when_f_expression_then_default_now_fast_pathed_and_stays_correct(db):
    """`then=`/`default=` accepting an `F()`/`CombinedExpression` (not just a bare literal)
    recurses through `_annotation_is_cacheable()`/`_case_leaf_values()` the same way any other
    nested Expression does."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=9.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(effective=Case(When(rating__gte=8, then=F("rating") * 2), default=F("rating")))
            .filter(name="alpha")
            .values("effective")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(effective=Case(When(rating__gte=8, then=F("rating") * 2), default=F("rating")))
            .filter(name="beta")
            .values("effective")
        )
    assert spy.call_count > 0
    assert first == [{"effective": 1.0}]
    assert second == [{"effective": 18.0}]


@pytest.mark.asyncio
async def test_case_none_default_is_fast_pathed_apart_from_a_literal_default(db):
    """An implicit (or explicit) `default=None` renders `ELSE NULL` with no parameter - part of the
    shape, so the query keeps a plan, and a `default` holding a literal has a plan of its own."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=9.0)

    def categories(name, default=None):
        return (
            Book.objects.annotate(category=Case(When(rating__gte=8, then="big"), default=default))
            .filter(name=name)
            .values("category")
        )

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await categories("alpha")
        assert spy.call_count == 0
        second = await categories("beta")
        assert spy.call_count == 1
        with_default = await categories("alpha", "small")
        assert spy.call_count == 1
    assert first == [{"category": None}]
    assert second == [{"category": "big"}]
    assert with_default == [{"category": "small"}]


# A run takes the compiled statement, whose query is the stored one itself, read-only; the
# clone this checks is the full build every other hit (.sql(), a subquery) takes.
@patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)
@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_case_when_tree(db):
    """Symmetry-principle regression test for `Case`/`When` - a cache hit must never leave the
    CACHED template's own `ValueWrapper` (embedded in either the WHEN-condition criterion or a
    THEN/ELSE literal) reachable from the freshly built query."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=9.0)
    await Book.objects.create(name="beta", author=author, rating=9.0)

    def all_value_wrapper_ids(query):
        ids = set()
        for select in query._selects:
            ids |= {id(node) for node in select.nodes_() if isinstance(node, ValueWrapper)}
        if query._wheres is not None:
            ids |= {id(node) for node in query._wheres.nodes_() if isinstance(node, ValueWrapper)}
        return ids

    first_query = (
        Book.objects.annotate(category=Case(When(rating__gte=8, then="big"), default="small"))
        .filter(name="alpha")
        .values("category")
    )
    first_query = _built(first_query)
    first_ids = all_value_wrapper_ids(first_query.query)

    second_query = (
        Book.objects.annotate(category=Case(When(rating__gte=8, then="big"), default="small"))
        .filter(name="beta")
        .values("category")
    )
    second_query = _built(second_query)
    second_ids = all_value_wrapper_ids(second_query.query)

    assert first_ids and second_ids
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_rawsql_annotation_now_fast_pathed_and_stays_correct(db):
    """`.annotate(x=RawSQL("...", [params]))` is now cacheable - unlike every other ref shape in
    this file, `RawSQL`'s own `.parameters` are ALREADY built `ValueWrapper` objects by construction
    time (`RawSQL.__init__` wraps them eagerly), so `_get_annotate()` records a
    `LiteralValueReference` for each directly, without a separate `.get_result()` step."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)

    with _spy_on_plan_hits_of_any_type() as spy:
        first = (
            await Book.objects.annotate(bumped=RawSQL("rating + %s", [10]))
            .filter(name="alpha")
            .values("name", "bumped")
        )
        assert spy.call_count == 0
        second = (
            await Book.objects.annotate(bumped=RawSQL("rating + %s", [10]))
            .filter(name="beta")
            .values("name", "bumped")
        )
    assert spy.call_count > 0
    assert first == [{"name": "alpha", "bumped": 11.0}]
    assert second == [{"name": "beta", "bumped": 12.0}]


@pytest.mark.asyncio
async def test_rawsql_annotation_param_is_substituted_correctly(db):
    """A DIFFERENT value passed as a `RawSQL(...)` param (via a Python variable, not a literal
    written differently in the source) must substitute correctly on a cache hit, not silently
    reuse the FIRST call's bind value."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    def build(offset):
        return Book.objects.annotate(bumped=RawSQL("rating + %s", [offset])).filter(name="alpha")

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await build(10).values("bumped")
        assert spy.call_count == 0
        second = await build(100).values("bumped")
    assert spy.call_count > 0
    assert first == [{"bumped": 11.0}]
    assert second == [{"bumped": 101.0}]


@pytest.mark.asyncio
async def test_rawsql_different_sql_text_is_part_of_the_cache_key(db):
    """Two `RawSQL(...)` annotations with DIFFERENT SQL text (not just different params) must
    never collide into one cache entry - the literal `.sql` text is part of
    `_annotation_shape_key()`'s own `RawSQL` branch."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    StatementPlans.plans.clear()
    await Book.objects.annotate(bumped=RawSQL("rating + %s", [1])).filter(name="alpha").values("bumped")
    entries_add = len(StatementPlans.plans)

    await Book.objects.annotate(bumped=RawSQL("rating * %s", [1])).filter(name="alpha").values("bumped")
    entries_mul = len(StatementPlans.plans)

    assert entries_mul == entries_add + 1


@pytest.mark.asyncio
async def test_rawsql_injection_shaped_value_stays_bound_across_cache_hits(db):
    """An injection-shaped value passed as a `RawSQL(...)` param must stay safely bind-
    parameterized (never string-interpolated into the SQL text) across a cache hit too - the
    whole point of this cache is that it substitutes VALUES, never SQL TEXT."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=2.0)
    malicious = "x'; DROP TABLE book; --"

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await Book.objects.annotate(tag=RawSQL("%s", [malicious])).filter(name="alpha").values("tag")
        assert spy.call_count == 0
        second = await Book.objects.annotate(tag=RawSQL("%s", [malicious])).filter(name="beta").values("tag")
    assert spy.call_count > 0
    assert first == [{"tag": malicious}]
    assert second == [{"tag": malicious}]
    assert await Book.objects.all().count() == 2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_function_with_reordered_database_func_args_does_not_crash_and_stays_correct(db):
    """Real, live-caught bug during this increment's implementation: the `Coalesce`/`NTile`
    literal-recording commit assumed `term.args[1:]` always lines up positionally with
    `self.default_values` (`term.args[0]` being the main field) - true for every function tested
    at the time, but NOT for `TruncYear`/`TruncMonth`/`TruncDay`
    (`hare/dialects/postgresql/functions/`'s `_DateTrunc.__init__(self, field)` calls
    `super().__init__("DATE_TRUNC", self.unit, field)`, putting its own hardcoded `self.unit`
    BEFORE `field`) - `TruncYear`'s own `default_values` is empty, so `zip(self.default_values,
    term.args[1:], strict=True)` raised `ValueError: zip() argument 2 is longer than argument 1`
    the moment this shape was cache-eligible (any `.values()`/`.annotate()` call). Fixed by only
    attempting the positional zip when `len(term.args) == 1 + len(self.default_values)` - a
    mismatch (this shape) skips recording gracefully instead of crashing, correctly matching the
    already-zero expected leaf count for `TruncYear`'s own empty `default_values`."""
    from datetime import datetime

    from hare.query.functions.datetime import TruncYear

    obj = await DatetimeFields.objects.create(datetime=datetime(2024, 3, 15, 12, 30))

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await DatetimeFields.objects.annotate(year=TruncYear("datetime")).filter(id=obj.id).values("year")
        assert spy.call_count == 0
        second = await DatetimeFields.objects.annotate(year=TruncYear("datetime")).filter(id=obj.id).values("year")
    assert spy.call_count > 0
    assert first == second
    assert first[0]["year"].year == 2024
    assert first[0]["year"].month == 1
    assert first[0]["year"].day == 1


@pytest.mark.asyncio
async def test_populate_field_object_annotation_decodes_correctly_on_a_cache_hit_too(db):
    """Real, live-caught bug: `_annotation_output_fields` (populated by `_get_annotate()` for any
    `populate_field_object` annotation - `Sum`/`Max`/`Min`/`Avg`/`Coalesce`/`Case`) is the ONLY
    thing that tells `.values()`/`.values_list()` to decode an annotation's result through its own
    field type (e.g. a `DecimalField`'s `from_db_value()`) instead of leaving it as the raw
    driver value - but every `QUERY_SHAPE_CACHE`-hit branch (`QuerySet`/`ValuesQuery`/
    `ValuesListQuery`/`AggregateQuery`) used to restore `_decode_plan`/`_select_related_idx` on a
    hit WITHOUT also restoring `_annotation_output_fields`, since only `_get_annotate()` (skipped
    entirely on a hit) ever populated it. The FIRST call of a given shape decoded correctly (cold
    path); every later structurally-identical call silently got back a raw, undecoded value
    instead - confirmed live returning a bare `float` instead of `Decimal` for a repeated
    `Max("decimal")` call, and a raw JSON string instead of a decoded `dict` for a repeated
    `Coalesce(json_field, ...)` call."""
    a = await DecimalFields.objects.create(decimal=Decimal("1.5000"), decimal_nodec=Decimal("1"))
    b = await DecimalFields.objects.create(decimal=Decimal("2.5000"), decimal_nodec=Decimal("2"))

    with _spy_on_plan_hits_of_any_type() as spy:
        first = await DecimalFields.objects.filter(id=a.id).annotate(m=Max("decimal")).values("m")
        assert spy.call_count == 0
        second = await DecimalFields.objects.filter(id=b.id).annotate(m=Max("decimal")).values("m")
    assert spy.call_count > 0
    assert first[0]["m"] == Decimal("1.5000")
    assert isinstance(first[0]["m"], Decimal)
    assert second[0]["m"] == Decimal("2.5000")
    assert isinstance(second[0]["m"], Decimal)

    obj_null = await JSONFields.objects.create(data={"fallback": True}, data_null=None)
    obj_set = await JSONFields.objects.create(data={"fallback": True}, data_null={"y": 2})

    with _spy_on_plan_hits_of_any_type() as spy:
        row_null = (
            await JSONFields.objects.filter(id=obj_null.id).annotate(x=Coalesce("data_null", F("data"))).values("x")
        )
        assert spy.call_count == 0
        row_set = (
            await JSONFields.objects.filter(id=obj_set.id).annotate(x=Coalesce("data_null", F("data"))).values("x")
        )
    assert spy.call_count > 0
    assert row_null[0]["x"] == {"fallback": True}
    assert row_set[0]["x"] == {"y": 2}


def _spy_on_exists_resolve():
    """`Exists.get_result()`'s own call count is the unambiguous signal for whether the OUTER query
    actually re-resolved the correlated/independent subquery this call, as opposed to a cache HIT
    on the OUTER skipping it entirely (see `Exists.get_result()`'s own docstring) - unlike
    `_spy_on_plan_hits()`/`_spy_on_plan_hits_of_any_type()`, a LOW count here (not a high one) is the "fast path
    engaged" signal for a query containing an Exists(...) annotation."""
    return patch.object(Exists, "get_result", side_effect=Exists.get_result, autospec=True)


@pytest.mark.asyncio
async def test_independent_exists_annotation_now_fast_pathed_and_stays_correct(db):
    """An `Exists(...)` annotation wrapping a fully INDEPENDENT inner queryset (no `OuterReference(...)`
    at all) is now cacheable - repeated OUTER calls with BOTH a different outer filter value AND
    a different inner filter value must still hit the cache (`Exists.get_result()` called only once)
    and each return the correct, independently-computed result."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)
    await Book.objects.create(name="gamma", author=author, rating=3.0)

    with _spy_on_exists_resolve() as resolve_spy, _spy_on_plan_hits() as clone_spy:
        rows1 = (
            await Book.objects.filter(name="alpha").annotate(any_gt=Exists(Book.objects.filter(rating__gt=0.5))).all()
        )
        rows2 = (
            await Book.objects.filter(name="beta").annotate(any_gt=Exists(Book.objects.filter(rating__gt=4.0))).all()
        )
        rows3 = (
            await Book.objects.filter(name="gamma")
            .annotate(any_gt=Exists(Book.objects.filter(rating__gt=100.0)))
            .all()
        )
    assert resolve_spy.call_count == 1, "Exists.get_result() must only run on the first, cache-populating call"
    assert clone_spy.call_count > 0
    assert len(rows1) == 1 and rows1[0].any_gt
    assert len(rows2) == 1 and rows2[0].any_gt
    assert len(rows3) == 1 and not rows3[0].any_gt
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_correlated_exists_outer_ref_runs_on_its_plan(db):
    """A real correlated subquery (`Exists(Other.objects.filter(field=OuterReference("id")))`) keeps a plan:
    `OuterReference("id")` reads the enclosing query's column and binds no value, so the outer query
    resolves the `Exists(...)` once - every later query runs on the plan, its rows still right."""
    t1 = await Tournament.objects.create(name="t1")
    await Tournament.objects.create(name="t2")
    reporter = await Reporter.objects.create(name="rep-a")
    await Event.objects.create(name="only-event-t1", tournament=t1, reporter=reporter)

    with _spy_on_exists_resolve() as resolve_spy:
        for _ in range(3):
            rows = (
                await Tournament.objects.annotate(
                    has_event=Exists(Event.objects.filter(tournament_id=OuterReference("id")))
                )
                .filter(name="t1")
                .all()
            )
            assert len(rows) == 1 and rows[0].has_event
            rows_none = (
                await Tournament.objects.annotate(
                    has_event=Exists(Event.objects.filter(tournament_id=OuterReference("id")))
                )
                .filter(name="t2")
                .all()
            )
            assert len(rows_none) == 1 and not rows_none[0].has_event
    assert resolve_spy.call_count == 1, "a correlated OuterReference(...) Exists() runs on the outer query's plan"
    assert [k for k in StatementPlans.plans if k[0] is Tournament]


@pytest.mark.asyncio
async def test_correlated_exists_outer_ref_related_field_crossing_join_is_cached_and_correct(db):
    """`OuterReference("events__reporter__name")` crosses a relation on the OUTER side (threaded back
    via `outer_extra_joins`, see `OuterReference.get_result()`'s own docstring) - the JOIN records its
    default scope like any other, so the query keeps a plan: repeated calls resolve the Exists once
    and each returns its own result."""
    t1 = await Tournament.objects.create(name="t1")
    await Tournament.objects.create(name="t2")
    reporter = await Reporter.objects.create(name="rep-a")
    await Event.objects.create(name="e1", tournament=t1, reporter=reporter)

    with _spy_on_exists_resolve() as resolve_spy:
        for name, expected_count in (("t1", 1), ("t2", 1), ("t1", 1)):
            rows = (
                await Tournament.objects.annotate(
                    has_event_for_reporter=Exists(
                        Event.objects.filter(reporter__name=OuterReference("events__reporter__name"))
                    )
                )
                .filter(name=name)
                .all()
            )
            assert len(rows) == expected_count
            assert rows[0].has_event_for_reporter is (name == "t1")
    assert resolve_spy.call_count == 1


@pytest.mark.asyncio
async def test_self_referential_exists_annotation_aliases_correctly(db):
    """`Exists(Book.objects.filter(...))` used as an annotation ON `Book` itself - `_apply_effective_
    basetable()`'s self-referential aliasing (see its own docstring) must still correctly alias
    the inner query's own base table, even though this ExistsQuery is now reached via
    `Exists.get_result()`'s threaded (own-cache-bypassing) path rather than its independent cache."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)

    qs = Book.objects.annotate(has_other=Exists(Book.objects.filter(rating__gt=0))).filter(name="alpha")
    built_qs = _built(qs)
    sql = built_qs.query.get_sql()
    assert "book__exists_inner" in sql, f"expected the inner table to be aliased, got: {sql}"

    rows = await qs.all()
    assert len(rows) == 1 and rows[0].has_other

    # Repeated calls (now a cache hit on the OUTER) must keep producing the aliased SQL - the
    # cache stores the FULLY-BUILT (already-aliased) template, not a shape that could silently
    # regress to unaliased on a later hit.
    qs2 = Book.objects.annotate(has_other=Exists(Book.objects.filter(rating__gt=0))).filter(name="beta")
    built_qs2 = _built(qs2)
    sql2 = built_qs2.query.get_sql()
    assert "book__exists_inner" in sql2


@pytest.mark.asyncio
async def test_no_stale_value_ids_survive_in_cloned_exists_tree(db):
    """The symmetry-principle regression test for `Exists(...)` annotations specifically: after a
    cache HIT with a DIFFERENT outer+inner value combination, no `ValueWrapper` `id()` from the
    FIRST call's cached template (living inside the `ExistsTerm`'s own `inner_query`) may survive
    into the SECOND call's actual query."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)

    qs_first = Book.objects.filter(name="alpha").annotate(any_gt=Exists(Book.objects.filter(rating__gt=0.5)))
    await qs_first.all()
    cached_query = next(iter(StatementPlans.plans.values())).template
    cached_ids = _value_wrapper_ids_including_exists(cached_query._wheres)
    for select_term in cached_query._selects:
        cached_ids |= {id(node) for node in select_term.nodes_() if isinstance(node, ValueWrapper)}
    assert cached_ids

    qs_second = Book.objects.filter(name="beta").annotate(any_gt=Exists(Book.objects.filter(rating__gt=4.0)))
    built_qs_second = _built(qs_second)
    fresh_ids = _value_wrapper_ids_including_exists(built_qs_second.query._wheres)
    for select_term in built_qs_second.query._selects:
        fresh_ids |= {id(node) for node in select_term.nodes_() if isinstance(node, ValueWrapper)}

    assert fresh_ids.isdisjoint(cached_ids), "a cloned Exists() tree must never reuse the cached template's own nodes"


@pytest.mark.asyncio
async def test_exists_with_cte_inner_is_cached_and_correct(db):
    """`Exists(...)` wrapping an inner queryset that itself has a `.with_cte(...)` is described as
    the exists query built into it - its CTE's values among the enclosing query's - so repeated
    calls resolve the Exists once, each with its own inner values."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)

    with _spy_on_exists_resolve() as resolve_spy:
        for minimum_rating, expected in ((0, True), (5, False), (0, True)):
            inner_with_cte = Book.objects.filter(rating__gt=minimum_rating).with_cte(
                "dummy", Book.objects.filter(rating__gt=999)
            )
            rows = await Book.objects.annotate(has_other=Exists(inner_with_cte)).filter(name="alpha").all()
            assert len(rows) == 1 and rows[0].has_other is expected
    assert resolve_spy.call_count == 1


@pytest.mark.asyncio
async def test_exists_self_referential_with_cte_inner_does_not_leak_correlation_alias_into_cte(db):
    """Regression test for a pre-existing bug found live while writing the test above (confirmed
    present even on an unmodified checkout, unrelated to QUERY_SHAPE_CACHE itself): a CTE
    attached via `.with_cte(...)` to a queryset that is ITSELF being resolved as the inner side
    of a SELF-REFERENTIAL `Exists(...)` (same model, so `_apply_effective_basetable()` aliases
    the ExistsQuery's own base table) used to have `outer_expression_context`/`outer_extra_joins`
    still ambiently active while `_apply_with_ctes()` built the CTE's own body - wrongly aliasing
    the CTE's UNRELATED, independently-scoped query the same way, producing a reference to an
    alias that exists nowhere in scope (Postgres rejects it outright: "missing FROM-clause entry
    for table ...__exists_inner"; SQLite silently accepts the same malformed SQL without
    erroring). `_apply_with_ctes()` now clears both contextvars for the duration of each nested
    CTE body's own `_make_query()` call - a CTE can never reference the enclosing query's own
    FROM items in standard SQL regardless of aliasing, so this is always correct, not just a
    self-referential special case."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=999.0)

    inner_with_cte = Book.objects.filter(rating__gt=0).with_cte("dummy", Book.objects.filter(rating__gt=500.0))
    qs = Book.objects.annotate(has_other=Exists(inner_with_cte)).filter(name="alpha")
    built_qs = _built(qs)
    sql = built_qs.query.get_sql()
    assert '"book__exists_inner"."rating"' not in sql.split("SELECT 1", 1)[0], (
        f"the CTE body must not reference the correlated ExistsTerm's own inner alias: {sql}"
    )
    rows = await qs.all()
    assert len(rows) == 1 and rows[0].has_other


@pytest.mark.asyncio
async def test_exists_annotation_referenced_in_filter_runs_on_its_plan(db):
    """`.annotate(has_x=Exists(...)).filter(has_x=True)` - the canonical usage shown in `Exists`'s
    own docstring - resolves the annotation twice while it is built (the SELECT list and the
    filter); a later query binds the inner query's values and the filter value of both."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)

    def books(minimum_rating: float, has_other: bool):
        return Book.objects.annotate(has_other=Exists(Book.objects.filter(rating__gt=minimum_rating))).filter(
            has_other=has_other
        )

    with _spy_on_exists_resolve() as resolve_spy:
        # Exists(...) isn't correlated - it is the same for every outer row.
        for minimum_rating, has_other, expected in (
            (4.0, True, {"alpha", "beta"}),
            (10.0, True, set()),
            (10.0, False, {"alpha", "beta"}),
            (4.0, False, set()),
        ):
            assert {book.name for book in await books(minimum_rating, has_other)} == expected
    assert resolve_spy.call_count == 2, "the annotation is resolved twice by the first build only"


@pytest.mark.asyncio
async def test_nested_exists_inside_exists_now_fast_pathed(db):
    """`Exists(...)` wrapping a queryset that ITSELF has an `Exists(...)` annotation - the
    recursive `_annotation_is_cacheable()`/`_annotation_leaf_count()`/`_annotation_leaf_values()`/
    `_annotation_shape_key()` machinery (reusing `_query_is_plannable()` against the inner
    queryset's own `_annotations`) handles this for free, the same way nested `.with_cte(...)`
    already does."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1.0)
    await Book.objects.create(name="beta", author=author, rating=5.0)
    await Book.objects.create(name="gamma", author=author, rating=9.0)

    def build(outer_name, mid_threshold, inner_threshold):
        mid = Book.objects.filter(rating__gt=mid_threshold).annotate(
            has_higher=Exists(Book.objects.filter(rating__gt=inner_threshold))
        )
        return Book.objects.filter(name=outer_name).annotate(mid_has_any=Exists(mid))

    with _spy_on_exists_resolve() as resolve_spy:
        # Both calls share the SAME structural shape (same nesting, same annotation keys) but
        # different threshold VALUES at every level (outer name, mid filter, inner filter) - the
        # second call must hit the OUTER's own cache (Exists.get_result() runs only once) and both
        # must still return the correct result: `mid` (books rated above mid_threshold) is
        # non-empty in both cases, so `mid_has_any` is True either way - `.exists()` only cares
        # whether `mid`'s OWN filter matches any row, `has_higher`'s per-row value never affects
        # that count, it's exercised here purely to prove the nested Exists-in-Exists shape is
        # itself recursively recognized as cacheable without crashing or miscounting leaves.
        rows1 = await build("alpha", 0.5, 8.0).all()
        rows2 = await build("beta", 4.0, 100.0).all()
    # Two Exists.get_result() calls happen on the FIRST (cache-populating) call alone - once for the
    # outer `Exists(mid)`, once more for `mid`'s own nested `Exists(...)` annotation, resolved via
    # the SAME threaded call. The second (cache-hit) call adds zero more.
    assert resolve_spy.call_count == 2, "second call (same shape) must hit the OUTER's own cache"
    assert len(rows1) == 1 and rows1[0].mid_has_any
    assert len(rows2) == 1 and rows2[0].mid_has_any
    assert len(StatementPlans.plans) == 1


@pytest.mark.asyncio
async def test_exists_nested_filter_across_ambient_scoped_relation_never_caches_across_tenants(db):
    """Real, live-caught cross-tenant leak: `ExistsQuery`/`CountQuery`/`AggregateQuery` inherit
    `StatementPlanDescriptions.query_is_plannable(AwaitableQuery)` directly (they are siblings of `QuerySet`, not
    subclasses of it - see `_spy_on_plan_hits_of_any_type()`'s own docstring), which has no
    ambient-scope (`Meta.tenant_field`/`Meta.soft_delete_field`) exclusion of its own - only
    `QuerySet`/`FieldSelectQuery` (`.values()`/`.values_list()`) had one. A `related__field=value`
    nested filter crossing to an ambient-scoped relation folds the ACTIVE tenant's id into the
    JOIN unthreaded (`Q._get_nested_filter()`'s own `ambient_condition` fold - see its docstring),
    exactly like the already-fixed `.values()` leak - so the FIRST caller's tenant froze into the
    cached JOIN forever, silently reused by every OTHER tenant sharing this same filter shape.
    `ExistsQuery`/`CountQuery`/`AggregateQuery` now each carry the same exclusion `FieldSelectQuery`
    already had."""
    widget_1 = await TenantScopedWidget.objects.create(name="shared", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="other", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        assert await TenantScopedOrder.objects.filter(widget__name="shared").exists() is True

    with Tenancy.scope(2):
        # SAME filter shape ("widget__name") as tenant 1's call above - a cache leak would
        # incorrectly still see tenant 1's widget here.
        assert await TenantScopedOrder.objects.filter(widget__name="shared").exists() is False


@pytest.mark.asyncio
async def test_count_nested_filter_across_ambient_scoped_relation_never_caches_across_tenants(db):
    """`CountQuery`'s own copy of the leak fixed in `test_exists_nested_filter_across_ambient_
    scoped_relation_never_caches_across_tenants` above."""
    widget_1 = await TenantScopedWidget.objects.create(name="shared", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="other", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    with Tenancy.scope(1):
        assert await TenantScopedOrder.objects.filter(widget__name="shared").count() == 1

    with Tenancy.scope(2):
        assert await TenantScopedOrder.objects.filter(widget__name="shared").count() == 0


@pytest.mark.asyncio
async def test_count_group_by_across_ambient_scoped_relation_builds_no_join(db):
    """`count()` ignores `.group_by()` - a `.group_by("related__field")` crossing an ambient-scoped
    relation builds no JOIN whose ambient condition a cached shape could freeze."""
    widget = await TenantScopedWidget.objects.create(name="shared", company_id=1)
    await TenantScopedOrder.objects.create(name="O1", widget=widget)

    with Tenancy.scope(1):
        count_query = TenantScopedOrder.objects.all().group_by("widget__name").count()
        assert "JOIN" not in count_query.sql()
        assert await TenantScopedOrder.objects.all().group_by("widget__name").count() == 1


# ============================================================================
# General invariant: cached-path SQL must always match an independent fresh rebuild
#
# Every QUERY_SHAPE_CACHE bug found so far (int-vs-float/Decimal literal conflation in
# CombinedExpression, a CTE leaking into a later query of the same base-filter shape, an
# annotate()'s own unconditional JOIN losing a select_related() extra_condition, a cross-tenant
# leak through a nested-filter JOIN) is one instance of the SAME underlying property being
# violated: the cache key didn't fully capture something that changes the SQL, so a later call
# with a different value for that "something" incorrectly reused an earlier call's cached
# structure. Rather than adding one more hand-written repro per newly-discovered gap, this test
# checks the property directly and generically: for a fuzzed matrix of query "recipes" spanning
# the dimensions that have actually caused a leak before (plus a few adjacent, similarly-shaped
# ones), each recipe's SQL/params produced while sharing a warm, cross-recipe-populated cache
# must be BYTE-IDENTICAL to that same recipe's own SQL/params from an independent, freshly-built
# (cache-cleared) query. A mismatch means some recipe silently inherited a different recipe's
# cached structure - exactly the failure mode every bug in this family has taken.
# ============================================================================


async def _sql_params(queryset: AwaitableQuery) -> tuple[str, list]:
    built_queryset = _built(queryset)
    return built_queryset.query.get_parameterized_sql()


@pytest.mark.asyncio
async def test_query_shape_cache_matches_fresh_rebuild_across_fuzzed_combinations(db):
    tournament_match = await Tournament.objects.create(name="Match")
    tournament_other = await Tournament.objects.create(name="Other")
    reporter = await Reporter.objects.create(name="r")
    await Event.objects.create(name="e1", tournament=tournament_match, reporter=reporter)
    await Event.objects.create(name="e2", tournament=tournament_other)
    await IntFields.objects.create(intnum=7)
    await Category.objects.create(name="root")
    await Category.objects.create(name="other", parent_id=1)
    widget_1 = await TenantScopedWidget.objects.create(name="w", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="w", company_id=2)

    async def literal_int_1():
        return await _sql_params(IntFields.objects.annotate(result=F("intnum") + 1).filter(intnum__gt=0))

    async def literal_int_2():
        # A different int VALUE, same shape - must share the int-literal (uncast) structure, not
        # collide with the float/Decimal recipes below.
        return await _sql_params(IntFields.objects.annotate(result=F("intnum") + 2).filter(intnum__gt=0))

    async def literal_float():
        return await _sql_params(IntFields.objects.annotate(result=F("intnum") + 0.5).filter(intnum__gt=0))

    async def literal_decimal():
        return await _sql_params(IntFields.objects.annotate(result=F("intnum") + Decimal("0.5")).filter(intnum__gt=0))

    async def cte_absent():
        return await _sql_params(Category.objects.filter(parent_id=1))

    async def cte_present_a():
        inner = Category.objects.filter(name="root")
        return await _sql_params(Category.objects.filter(parent_id=1).with_cte("only_root", inner))

    async def cte_present_b():
        # A different inner CTE body/literal, same outer filter shape.
        inner = Category.objects.filter(name="other")
        return await _sql_params(Category.objects.filter(parent_id=2).with_cte("only_root", inner))

    async def extra_condition_absent():
        return await _sql_params(Event.objects.filter(name="e1").annotate(tname=F("tournament__name")))

    async def extra_condition_present_a():
        qs = Event.objects.filter(name="e1").select_related(Select("tournament", extra_condition=Q(name="Match")))
        return await _sql_params(qs.annotate(tname=F("tournament__name")))

    async def extra_condition_present_b():
        # A different extra_condition literal, same outer filter shape.
        qs = Event.objects.filter(name="e2").select_related(Select("tournament", extra_condition=Q(name="Other")))
        return await _sql_params(qs.annotate(tname=F("tournament__name")))

    async def tenant_1():
        with Tenancy.scope(1):
            return await _sql_params(TenantScopedWidget.objects.filter(name="w"))

    async def tenant_2():
        with Tenancy.scope(2):
            return await _sql_params(TenantScopedWidget.objects.filter(name="w"))

    async def distinct_on_name():
        return await _sql_params(Event.objects.all().distinct("name").order_by("name"))

    async def distinct_on_id():
        return await _sql_params(Event.objects.all().distinct("event_id").order_by("event_id"))

    async def window_partition_by_tournament():
        return await _sql_params(
            Event.objects.annotate(rn=Window(RowNumber(), partition_by=["tournament_id"], order_by=["name"]))
        )

    async def window_partition_by_reporter():
        return await _sql_params(
            Event.objects.annotate(rn=Window(RowNumber(), partition_by=["reporter_id"], order_by=["name"]))
        )

    recipes = {
        "literal_int_1": literal_int_1,
        "literal_int_2": literal_int_2,
        "literal_float": literal_float,
        "literal_decimal": literal_decimal,
        "cte_absent": cte_absent,
        "cte_present_a": cte_present_a,
        "cte_present_b": cte_present_b,
        "extra_condition_absent": extra_condition_absent,
        "extra_condition_present_a": extra_condition_present_a,
        "extra_condition_present_b": extra_condition_present_b,
        "tenant_1": tenant_1,
        "tenant_2": tenant_2,
        "window_partition_by_tournament": window_partition_by_tournament,
        "window_partition_by_reporter": window_partition_by_reporter,
    }

    probe_queryset = Event.objects.all()._get_execution_query()
    if probe_queryset.dialect.name == "postgresql":
        # DISTINCT ON is PostgreSQL-only (OperationalError on every other dialect) - only add
        # this dimension when this test run's real backend can actually build it.
        recipes["distinct_on_name"] = distinct_on_name
        recipes["distinct_on_id"] = distinct_on_id

    StatementPlans.plans.clear()
    warm_results = {label: await recipe() for label, recipe in recipes.items()}

    for label, recipe in recipes.items():
        StatementPlans.plans.clear()
        fresh = await recipe()
        assert fresh == warm_results[label], (
            f"{label!r}: SQL/params built while sharing a warm, cross-recipe cache diverged from "
            f"this same recipe's own independent, freshly-built (cache-cleared) query - some "
            f"OTHER recipe's cached structure leaked into this one.\n"
            f"warm:  {warm_results[label]}\n"
            f"fresh: {fresh}"
        )

    # Both widgets must still be reachable outside this test's own tenant scopes - a sanity check
    # that the recipes above didn't leave any ambient Tenancy state behind.
    assert widget_1.company_id == 1
    assert widget_2.company_id == 2


@pytest.mark.asyncio
async def test_fractional_filter_value_after_an_integer_one_of_the_same_shape_is_not_truncated(db):
    """The shape key ignored a filter value's TYPE, so filter(intnum=2.5) hit the entry cached by
    an earlier filter(intnum=1) and was rebound through to_db_value() - truncating 2.5 to 2 and
    returning the intnum=2 row instead of nothing, unlike a cold build (to_lookup_value())."""
    for intnum in range(5):
        await IntFields.objects.create(intnum=intnum)

    assert await IntFields.objects.filter(intnum=1).values_list("intnum", flat=True) == [1]
    assert await IntFields.objects.filter(intnum=2.5).values_list("intnum", flat=True) == []
    assert await IntFields.objects.filter(intnum=Decimal("3.5")).values_list("intnum", flat=True) == []
    assert await IntFields.objects.filter(intnum=2).values_list("intnum", flat=True) == [2]


@pytest.mark.asyncio
async def test_decimal_filter_value_is_not_quantized_on_a_cache_hit(db):
    """Same type (Decimal) on the same shape, so only the hit-path conversion matters: a
    DecimalField(decimal_places=0) filter must compare against Decimal("1.5") itself (matching
    nothing), not the write-time-quantized 2."""
    for value in range(4):
        await DecimalFields.objects.create(decimal=Decimal(value), decimal_nodec=Decimal(value))

    assert len(await DecimalFields.objects.filter(decimal_nodec=Decimal("1"))) == 1
    assert await DecimalFields.objects.filter(decimal_nodec=Decimal("1.5")) == []
    assert await DecimalFields.objects.filter(decimal_nodec=Decimal("1.5")) == []


@pytest.mark.asyncio
async def test_repeated_bare_value_annotation_returns_each_calls_own_literal(db):
    """A bare top-level Value(...) annotation is SELECTed through .as_(), which copies the
    ValueWrapper the cache recorded a ref to - a later call's literal was never substituted, so
    the FIRST call's literal came back every time."""
    row = await IntFields.objects.create(intnum=1)

    first = await IntFields.objects.filter(id=row.id).annotate(marker=Value(1)).values_list("id", "marker")
    second = await IntFields.objects.filter(id=row.id).annotate(marker=Value("x")).values_list("id", "marker")
    third = await IntFields.objects.filter(id=row.id).annotate(marker=Value(7)).values_list("id", "marker")

    assert first == [(row.id, 1)]
    assert second == [(row.id, "x")]
    assert third == [(row.id, 7)]


@pytest.mark.asyncio
async def test_date_part_filter_with_a_numeric_string_after_an_int_one_matches_a_cold_build(db):
    """date__year coerces its value to int when the criterion is built - a cache hit bypassed
    that and bound the raw string, matching nothing on SQLite."""
    for year in (2019, 2020, 2020):
        await DateFields.objects.create(date=datetime.date(year, 1, 1))

    assert len(await DateFields.objects.filter(date__year=2020)) == 2
    assert len(await DateFields.objects.filter(date__year="2020")) == 2


@pytest.mark.asyncio
async def test_json_has_key_lookups_stay_correct_across_cached_calls(db):
    """`__has_key` (`string_encoder`, a bare `ValueWrapper` right-hand side - fast-pathed as an
    `EncodedValueReference`) and `__has_keys`/`__has_any_keys` (an `Array(...)` right-hand side, built
    again from a later list of the same length - a plan per lookup and length): either way, a
    second call with a DIFFERENT key must never reuse the first call's key."""
    a = await JSONFields.objects.create(data={"a": 1, "b": 2})
    b = await JSONFields.objects.create(data={"c": 3})

    # On SQLite the lookup is a UDF call holding the key as its one parameter.
    with _spy_on_plan_hits() as spy:
        first = await JSONFields.objects.filter(data__has_key="a").all()
        second = await JSONFields.objects.filter(data__has_key="c").all()
        third = await JSONFields.objects.filter(data__has_key="a").all()
    assert [obj.id for obj in first] == [a.id]
    assert [obj.id for obj in second] == [b.id]
    assert [obj.id for obj in third] == [a.id]
    assert spy.call_count > 0
    assert len(StatementPlans.plans) == 1

    StatementPlans.plans.clear()
    with _spy_on_plan_hits() as spy:
        first = await JSONFields.objects.filter(data__has_keys=["a", "b"]).all()
        second = await JSONFields.objects.filter(data__has_keys=["c"]).all()
        third = await JSONFields.objects.filter(data__has_any_keys=["b", "zzz"]).all()
        fourth = await JSONFields.objects.filter(data__has_any_keys=["c", "zzz"]).all()
    assert [obj.id for obj in first] == [a.id]
    assert [obj.id for obj in second] == [b.id]
    assert [obj.id for obj in third] == [a.id]
    assert [obj.id for obj in fourth] == [b.id]
    # The fourth call runs on the plan of the third: __has_any_keys of two keys.
    assert spy.call_count == 1
    assert len(StatementPlans.plans) == 3


@pytest.mark.asyncio
async def test_get_by_pk_honors_lazy_joined(db):
    """`.get(pk=...)`'s fast path bypasses _make_query() (and therefore
    _apply_lazy_relation_defaults()) entirely - it must still end up in the same state a plain
    .filter(pk=...).first() would for a lazy="joined" relation."""
    parent = await LazyJoinedParent.objects.create(name="Parent")
    child = await LazyJoinedChild.objects.create(name="Child", parent=parent)

    via_get = await LazyJoinedChild.objects.get(pk=child.id)
    via_filter = await LazyJoinedChild.objects.filter(pk=child.id).first()

    assert isinstance(via_get.parent, LazyJoinedParent)
    assert via_get.parent.name == "Parent"
    assert set(via_get.__dict__.keys()) == set(via_filter.__dict__.keys())


@pytest.mark.asyncio
async def test_get_by_pk_honors_lazy_select(db):
    parent = await LazySelectParent.objects.create(name="Parent")
    child = await LazySelectChild.objects.create(name="Child", parent=parent)

    via_get = await LazySelectChild.objects.get(pk=child.id)

    assert via_get.parent.name == "Parent"


@pytest.mark.asyncio
async def test_get_by_pk_runs_on_its_plan_for_plain_models(db):
    """Regression control: on a model with no lazy= relations Model.objects.get() must still run on
    its shape's plan without building a query - this fix must not blanket-disable it for everyone."""
    from unittest.mock import patch

    from tests.testmodels import Author

    author = await Author.objects.create(name="a")
    # The first query of the shape builds its QuerySet, which records the plan.
    await Author.objects.get(pk=author.id)
    with patch.object(
        type(Author.objects.all()), "_make_query", side_effect=type(Author.objects.all())._make_query, autospec=True
    ) as spy:
        fetched = await Author.objects.get(pk=author.id)
        assert fetched.name == "a"
        spy.assert_not_called()


@pytest.mark.asyncio
async def test_select_related_idx_survives_query_shape_cache_hit(db):
    """Not a bug (verified false positive during review) - _select_related_idx is
    populated before the cache-hit early-return in _make_query(), so it's identical on a hit
    and a miss. Kept as a permanent regression guard against this changing by accident."""
    from tests.testmodels import Author

    await Author.objects.create(name="a")

    qs1 = Author.objects.filter(name="a")
    built_qs1 = _built(qs1)
    miss_idx = built_qs1._select_related_positions

    qs2 = Author.objects.filter(name="a")
    built_qs2 = _built(qs2)

    assert len(StatementPlans.plans) == 1
    assert built_qs2._select_related_positions == miss_idx
    assert built_qs2._select_related_positions != []


@pytest.mark.asyncio
async def test_alias_and_annotate_with_the_same_expression_do_not_share_a_cache_entry(db):
    """An .alias() and an .annotate() of one expression under one name used to share a cache
    entry, so the annotation reused the alias's SQL with no column for it."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="b", author=author, rating=1.0)

    aliased = await Book.objects.all().alias(bumped=F("rating") + 1).filter(name="b")
    assert len(aliased) == 1
    annotated = await Book.objects.annotate(bumped=F("rating") + 1).filter(name="b")
    assert annotated[0].bumped == 2.0
    aliased_again = Book.objects.all().alias(bumped=F("rating") + 1).filter(name="b")
    assert '"bumped"' not in aliased_again.sql()

    assert await Book.objects.all().alias(bumped=F("rating") + 1).values_list("bumped", flat=True) == [2.0]
    assert await Book.objects.annotate(bumped=F("rating") + 1).values_list("bumped", flat=True) == [2.0]
    assert await Book.objects.all().alias(bumped=F("rating") + 1).aggregate(total=Sum("bumped")) == {"total": 2.0}
    assert await Book.objects.annotate(bumped=F("rating") + 1).aggregate(total=Sum("bumped")) == {"total": 2.0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_query",
    [
        lambda queryset: queryset.values("name"),
        lambda queryset: queryset.values_list("name", flat=True),
        lambda queryset: queryset.count(),
        lambda queryset: queryset.exists(),
    ],
)
async def test_cache_hit_drops_a_previous_calls_cte(db, make_query):
    """A cache hit used to keep the WITH clause of the call that populated the entry whenever
    the current call had no CTE of its own."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="b", author=author, rating=1.0)

    await make_query(
        Book.objects.all().with_cte("stale_cte", Author.objects.filter(name="first_call")).filter(name="b")
    )
    later_query = make_query(Book.objects.filter(name="b"))

    assert "stale_cte" not in later_query.sql()
    await later_query
    assert "stale_cte" not in later_query.sql()
