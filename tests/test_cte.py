import uuid

import pytest

from hare.exceptions import (
    QueryError,
)
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import RawSQL
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.scopes.row_scopes import RowScopes
from hare.sql import Table
from hare.time import Timezone
from tests.testmodels import Author, Book, Category, SoftDeleteSelfReferentialChain, UuidTenantDoc


def _ancestors_cte(root_id: int):
    """Builds a `WITH RECURSIVE ancestors AS (...)` body walking Category.parent_id upward from
    root_id - using the model's own dialect-correct query_class (not the dialect-generic
    hare.sql.Query directly), matching with_cte()'s own documented convention."""
    query_cls = Category._meta.connection.query_class
    category = Table("category")
    ancestors = Table("ancestors")
    base = query_cls.from_(category).select(category.id, category.parent_id).where(category.id == root_id)
    step = (
        query_cls.from_(category)
        .join(ancestors)
        .on(category.id == ancestors.parent_id)
        .select(category.id, category.parent_id)
    )
    union_all = base * step
    # SQLite's grammar doesn't allow parenthesizing a compound-SELECT's own operands - matches
    # UnionQuery's own convention, safe/portable on both dialects.
    union_all.base_query.wrap_set_operation_queries = False
    return union_all


@pytest.mark.asyncio
async def test_with_cte_basic_attachment(db):
    """A plain (non-recursive) CTE, referenced via RawSQL in a normal .filter()."""
    await Category.objects.create(name="root")
    other = await Category.objects.create(name="other")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    only_root = query_cls.from_(category).select(category.id).where(category.name == "root")

    results = (
        await Category.objects.all()
        .with_cte("only_root", only_root)
        .filter(id__in=RawSQL('SELECT id FROM "only_root"'))
    )

    assert [c.name for c in results] == ["root"]
    assert other.name not in [c.name for c in results]


@pytest.mark.asyncio
async def test_with_cte_body_prefetch_related_raises_configuration_error(db):
    """A CTE body is never awaited on its own - with_cte() only ever takes its compiled
    Selectable (query.query) and folds it into a WITH clause, so a .prefetch_related() on the
    body silently never runs its follow-up queries. Previously this left the FK/M2M attribute on
    a hydrated row as an unfetched QuerySet stand-in instead of the real related object, with no
    error anywhere near the actual cause - mirrors the identical ConfigurationError union()
    already raises for the same "this loading strategy has no path left to actually run"
    problem."""
    author = await Author.objects.create(name="Tolkien")
    book = await Book.objects.create(name="LOTR", author=author, rating=5)

    cte_body = Book.objects.all().prefetch_related("author").filter(id=book.id)
    with pytest.raises(QueryError, match="CTE bodies do not support"):
        await Book.objects.all().with_cte("cte_books", cte_body).filter(id__in=RawSQL('SELECT id FROM "cte_books"'))


@pytest.mark.asyncio
async def test_with_cte_body_select_related_raises_configuration_error(db):
    """Same guard, .select_related() side - select_related() on a CTE body doesn't blow up the
    same way (the joined columns DO make it into the compiled SQL, just under an internal alias
    convention no caller is expected to know), but it's still a loading strategy nobody asked for
    that can never resolve through the normal Model._init_from_db() path - reject it the same way
    as prefetch_related() rather than silently succeeding on an undocumented alias."""
    author = await Author.objects.create(name="Tolkien")
    await Book.objects.create(name="LOTR", author=author, rating=5)

    cte_body = Book.objects.all().select_related("author")
    with pytest.raises(QueryError, match="CTE bodies do not support"):
        await Book.objects.all().with_cte("cte_books", cte_body).filter(id__in=RawSQL('SELECT id FROM "cte_books"'))


@pytest.mark.asyncio
async def test_with_cte_name_cannot_break_out_of_the_with_clause(db):
    """A CTE name spliced unquoted into "WITH {name} AS (...)" let a malicious name close the
    CTE early and substitute a completely different query - confirmed live before this fix that
    this exact payload replaced the whole SELECT/FROM and returned rows from an unrelated table."""
    await Category.objects.create(name="root")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    inner = query_cls.from_(category).select(category.id)

    evil_name = 'x AS (SELECT 1) SELECT "name" AS secret FROM "category" --'
    results = await Category.objects.all().with_cte(evil_name, inner).values("name")

    assert results == [{"name": "root"}]


@pytest.mark.asyncio
async def test_with_cte_recursive_tree_walk(db):
    """The actual target use case: one round trip instead of N+1 application-level recursion
    to walk a self-referential tree."""
    root = await Category.objects.create(name="root")
    child1 = await Category.objects.create(name="child1", parent_id=root.id)
    child2 = await Category.objects.create(name="child2", parent_id=child1.id)
    unrelated = await Category.objects.create(name="unrelated")

    results = (
        await Category.objects.all()
        .with_cte("ancestors", _ancestors_cte(child2.id))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
        .order_by("id")
    )

    assert [c.name for c in results] == ["root", "child1", "child2"]
    assert unrelated.name not in [c.name for c in results]


@pytest.mark.asyncio
async def test_with_cte_propagates_to_values_and_values_list(db):
    root = await Category.objects.create(name="root")
    child1 = await Category.objects.create(name="child1", parent_id=root.id)
    await Category.objects.create(name="unrelated")

    qs = (
        Category.objects.all()
        .with_cte("ancestors", _ancestors_cte(child1.id))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
    )

    names = await qs.values_list("name", flat=True)
    assert sorted(names) == ["child1", "root"]

    rows = await qs.values("name")
    assert sorted(row["name"] for row in rows) == ["child1", "root"]


@pytest.mark.asyncio
async def test_with_cte_propagates_to_count_and_exists(db):
    root = await Category.objects.create(name="root")
    child1 = await Category.objects.create(name="child1", parent_id=root.id)
    await Category.objects.create(name="unrelated")

    qs = (
        Category.objects.all()
        .with_cte("ancestors", _ancestors_cte(child1.id))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
    )

    assert await qs.count() == 2
    assert await qs.exists() is True

    empty_qs = (
        Category.objects.all()
        .with_cte("ancestors", _ancestors_cte(-1))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
    )
    assert await empty_qs.count() == 0
    assert await empty_qs.exists() is False


@pytest.mark.asyncio
async def test_with_cte_propagates_to_update(db):
    """.with_cte() used to be silently dropped by .update() - the CTE-referencing filter crashed
    with a raw driver error ("no such table"/"relation ... does not exist") instead of running the
    UPDATE against the rows the CTE actually names."""
    root = await Category.objects.create(name="root")
    other = await Category.objects.create(name="other")

    qs = (
        Category.objects.all()
        .with_cte("ancestors", _ancestors_cte(root.id))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
    )
    updated_count = await qs.update(name="renamed-root")
    assert updated_count == 1

    await root.refresh_from_db()
    await other.refresh_from_db()
    assert root.name == "renamed-root"
    assert other.name == "other"


@pytest.mark.asyncio
async def test_with_cte_propagates_to_delete(db):
    """Same gap as test_with_cte_propagates_to_update, on .delete()."""
    root = await Category.objects.create(name="root")
    await Category.objects.create(name="other")

    qs = (
        Category.objects.all()
        .with_cte("ancestors", _ancestors_cte(root.id))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
    )
    deleted_count = await qs.delete()
    assert deleted_count == 1

    remaining = await Category.objects.all().values_list("name", flat=True)
    assert list(remaining) == ["other"]


@pytest.mark.asyncio
async def test_with_cte_propagates_to_union_single_branch(db):
    """.with_cte() on only ONE branch of a union() used to render its WITH clause in the MIDDLE
    of the combined SQL text (right after "UNION", before the other branch's own SELECT) instead
    of at the very start - invalid SQL on every dialect. The CTE is now hoisted onto the combined
    query as a whole."""
    await Category.objects.create(name="root")
    other = await Category.objects.create(name="other")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    only_root = query_cls.from_(category).select(category.id).where(category.name == "root")

    qs1 = Category.objects.all().with_cte("only_root", only_root).filter(id__in=RawSQL('SELECT id FROM "only_root"'))
    qs2 = Category.objects.filter(id=other.id)

    result = await qs1.union(qs2)
    assert {c.name for c in result} == {"root", "other"}


@pytest.mark.asyncio
async def test_with_cte_propagates_to_union_both_branches(db):
    """Each branch registers its OWN, differently-named CTE - both must survive, hoisted
    together onto the combined query rather than either one being dropped or misplaced."""
    await Category.objects.create(name="root")
    await Category.objects.create(name="other_root")
    await Category.objects.create(name="unrelated")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    only_root = query_cls.from_(category).select(category.id).where(category.name == "root")
    only_other_root = query_cls.from_(category).select(category.id).where(category.name == "other_root")

    qs1 = Category.objects.all().with_cte("cte1", only_root).filter(id__in=RawSQL('SELECT id FROM "cte1"'))
    qs2 = Category.objects.all().with_cte("cte2", only_other_root).filter(id__in=RawSQL('SELECT id FROM "cte2"'))

    result = await qs1.union(qs2)
    assert {c.name for c in result} == {"root", "other_root"}


@pytest.mark.asyncio
async def test_with_cte_union_same_name_different_body_raises_configuration_error(db):
    """Two branches naming their own with_cte() the same but with genuinely different bodies is
    ambiguous - which body should the combined query's single WITH clause actually use? - so it
    raises a clear error instead of silently picking one and ignoring the other."""
    await Category.objects.create(name="root")
    await Category.objects.create(name="other_root")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    only_root = query_cls.from_(category).select(category.id).where(category.name == "root")
    only_other_root = query_cls.from_(category).select(category.id).where(category.name == "other_root")

    qs1 = (
        Category.objects.all().with_cte("shared_name", only_root).filter(id__in=RawSQL('SELECT id FROM "shared_name"'))
    )
    qs2 = (
        Category.objects.all()
        .with_cte("shared_name", only_other_root)
        .filter(id__in=RawSQL('SELECT id FROM "shared_name"'))
    )

    with pytest.raises(QueryError, match="defined differently"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_with_cte_propagates_to_union_count(db):
    """UnionCountQuery wraps the same combined SetOperationQuery as a subquery - must pick up
    the exact same hoisted WITH clause as a plain await of the union(), not silently diverge."""
    await Category.objects.create(name="root")
    other = await Category.objects.create(name="other")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    only_root = query_cls.from_(category).select(category.id).where(category.name == "root")

    qs1 = Category.objects.all().with_cte("only_root", only_root).filter(id__in=RawSQL('SELECT id FROM "only_root"'))
    qs2 = Category.objects.filter(id=other.id)

    assert await qs1.union(qs2).count() == 2


@pytest.mark.asyncio
async def test_with_cte_isolated_across_clones(db):
    """.with_cte() must not leak onto a sibling clone built from the same base queryset -
    the same mutable-container-aliasing class of bug this session already found/fixed for
    _select_related/_prefetch_queries."""
    root = await Category.objects.create(name="root")
    base_qs = Category.objects.all()
    with_cte_qs = base_qs.with_cte("only_root", _ancestors_cte(root.id))

    base_qs = base_qs._get_compiler()._get_execution_query()
    base_qs._make_query()
    assert not base_qs._with_ctes

    with_cte_qs = with_cte_qs._get_compiler()._get_execution_query()
    with_cte_qs._make_query()
    assert len(with_cte_qs._with_ctes) == 1


@pytest.mark.asyncio
async def test_with_cte_does_not_leak_into_a_later_unrelated_query_of_the_same_shape(db):
    """QUERY_SHAPE_CACHE's cache-hit branch only rebuilt query._with fresh from THIS call's own
    with_cte() state when self._with_ctes was truthy - a later, SEPARATE query sharing the same
    filter shape but with NO with_cte() call at all skipped that reset entirely, so it silently
    inherited whatever WITH clause (and embedded literal values) the FIRST call that populated
    this cache entry happened to attach. Deliberately two independently-built querysets (not
    clones of one another - test_with_cte_isolated_across_clones already covers that narrower,
    already-fixed aliasing case)."""

    root = await Category.objects.create(name="root")
    StatementPlans.plans.clear()

    with_cte_qs = Category.objects.filter(parent_id=root.id).with_cte("only_root", _ancestors_cte(root.id))
    with_cte_qs = with_cte_qs._get_compiler()._get_execution_query()
    with_cte_qs._make_query()

    plain_qs = Category.objects.filter(parent_id=root.id)
    plain_qs = plain_qs._get_compiler()._get_execution_query()
    plain_qs._make_query()
    sql, params = plain_qs.query.get_parameterized_sql()

    assert "WITH" not in sql
    assert "only_root" not in sql
    assert params == [root.id]


@pytest.mark.asyncio
async def test_rawsql_in_lookup_is_parenthesized(db):
    """Regression test: RawSQL used as an __in= container used to render unparenthesized
    ("id" IN SELECT ... instead of "id" IN (SELECT ...)) - invalid SQL on every dialect."""
    await Category.objects.create(name="root")
    await Category.objects.create(name="other")

    query_cls = Category._meta.connection.query_class
    category = Table("category")
    only_root = query_cls.from_(category).select(category.id).where(category.name == "root")

    qs = Category.objects.all().with_cte("only_root", only_root).filter(id__in=RawSQL('SELECT id FROM "only_root"'))
    sql = qs.sql()
    assert 'IN (SELECT id FROM "only_root")' in sql

    results = await qs
    assert [c.name for c in results] == ["root"]


def _chain_ancestors_cte(leaf_id: int, *, apply_ambient_scope: bool):
    """Same shape as _ancestors_cte() above, but walking SoftDeleteSelfReferentialChain.parent_id
    - apply_ambient_scope controls whether RowScopes.get_criterion() gets folded
    into the base case/recursive step, to directly compare the buggy and fixed behavior."""
    query_cls = SoftDeleteSelfReferentialChain._meta.connection.query_class
    chain = Table("softdeleteselfreferentialchain")
    ancestors = Table("ancestors")
    scope = (
        RowScopes.of(SoftDeleteSelfReferentialChain).get_criterion(
            chain,
            dialect=SoftDeleteSelfReferentialChain._meta.connection.dialect,
            connection=None,
        )
        if apply_ambient_scope
        else None
    )
    base_where = chain.id == leaf_id
    step_where = chain.id == ancestors.parent_id
    if scope is not None:
        base_where = base_where & scope
        step_where = step_where & scope
    base = query_cls.from_(chain).select(chain.id, chain.parent_id).where(base_where)
    step = query_cls.from_(chain).join(ancestors).on(step_where).select(chain.id, chain.parent_id)
    union_all = base * step
    union_all.base_query.wrap_set_operation_queries = False
    return union_all


@pytest.mark.asyncio
async def test_with_cte_recursive_step_respects_soft_delete_ambient_scope(db):
    """A recursive CTE's own step never goes through Manager.get_queryset(), so it used to walk
    straight through a soft-deleted row sitting in the MIDDLE of the chain - that row itself was
    still correctly excluded by the outer queryset's own default filtering, but everything
    reachable only by tunneling THROUGH it (root, here) leaked into the result regardless.
    RowScopes.get_criterion(), folded into both the base case and the recursive
    step (see with_cte()'s own docstring example), fixes this - verified against both the
    unfixed and fixed shape of the SAME query, on the SAME data, to isolate the exact effect."""
    root = await SoftDeleteSelfReferentialChain.objects.create(name="root")
    middle = await SoftDeleteSelfReferentialChain.objects.create(name="middle", parent=root)
    leaf = await SoftDeleteSelfReferentialChain.objects.create(name="leaf", parent=middle)

    # Soft-deletes only "middle" directly via a hand-built UPDATE (bypassing .delete()'s own
    # CASCADE, which would otherwise soft-delete leaf too, since it's middle's child - that
    # would hide this bug entirely, since leaf itself would then also fail the outer filter for
    # an unrelated, correct reason - and bypassing the ORM's own deliberate guards against
    # mutating soft_delete_field directly via .update()/.save()). query_cls, not hare.sql.Query
    # directly, for the same dialect-correct-placeholder reasoning _chain_ancestors_cte() itself
    # already documents.
    query_cls = SoftDeleteSelfReferentialChain._meta.connection.query_class
    chain = Table("softdeleteselfreferentialchain")
    soft_delete_middle = query_cls.update(chain).set(chain.deleted_at, Timezone.now()).where(chain.id == middle.pk)
    sql, params = soft_delete_middle.get_parameterized_sql()
    await SoftDeleteSelfReferentialChain._meta.connection.execute(sql, params)

    unfixed = (
        await SoftDeleteSelfReferentialChain.objects.all()
        .with_cte("ancestors", _chain_ancestors_cte(leaf.pk, apply_ambient_scope=False))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
        .order_by("id")
    )
    assert sorted(c.name for c in unfixed) == ["leaf", "root"], (
        "documents the bug shape: root leaks through the soft-deleted middle row"
    )

    fixed = (
        await SoftDeleteSelfReferentialChain.objects.all()
        .with_cte("ancestors", _chain_ancestors_cte(leaf.pk, apply_ambient_scope=True))
        .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
        .order_by("id")
    )
    assert [c.name for c in fixed] == ["leaf"], "root must be unreachable once middle is soft-deleted"


@pytest.mark.asyncio
async def test_with_cte_over_a_none_queryset_is_empty(db):
    """A .none() CTE body holds no rows instead of every row."""
    await Category.objects.create(name="root")
    await Category.objects.create(name="other")

    empty = (
        await Category.objects.all()
        .with_cte("picked", Category.objects.none().values("id"))
        .filter(id__in=RawSQL('SELECT id FROM "picked"'))
    )
    full = (
        await Category.objects.all()
        .with_cte("picked", Category.objects.all().values("id"))
        .filter(id__in=RawSQL('SELECT id FROM "picked"'))
    )

    assert empty == []
    assert sorted(category.name for category in full) == ["other", "root"]


@pytest.mark.asyncio
async def test_ambient_scope_raw_criterion_encodes_a_uuid_tenant_through_the_field(db):
    """RowScopes.get_criterion() binds the tenant through the tenant field's own
    to_db_value() - a raw uuid.UUID used to be unbindable on SQLite."""
    org = uuid.uuid4()
    with Tenancy.scope(org):
        root = await UuidTenantDoc.objects.create(title="root")
        leaf = await UuidTenantDoc.objects.create(title="leaf", parent=root)
        query_cls = UuidTenantDoc._meta.connection.query_class
        doc = Table("uuidtenantdoc")
        ancestors = Table("ancestors")
        scope = RowScopes.of(UuidTenantDoc).get_criterion(
            doc, dialect=UuidTenantDoc._meta.connection.dialect, connection=None
        )
        base = query_cls.from_(doc).select(doc.id, doc.parent_id).where((doc.id == leaf.pk) & scope)
        step = (
            query_cls.from_(doc)
            .join(ancestors)
            .on((doc.id == ancestors.parent_id) & scope)
            .select(doc.id, doc.parent_id)
        )
        union_all = base * step
        union_all.base_query.wrap_set_operation_queries = False
        walked = await (
            UuidTenantDoc.objects.all()
            .with_cte("ancestors", union_all)
            .filter(id__in=RawSQL('SELECT id FROM "ancestors"'))
            .order_by("id")
        )
    assert [node.title for node in walked] == ["root", "leaf"]


def _high_rated_books():
    """Books filtered through a `high_rated` CTE holding the ids of books rated 4 or more."""
    return (
        Book.objects.all()
        .with_cte("high_rated", Book.objects.filter(rating__gte=4).values("id"))
        .filter(id__in=RawSQL('SELECT id FROM "high_rated"'))
    )


@pytest.mark.asyncio
async def test_with_cte_contains_keeps_cte(db):
    """contains() used to build its query without the queryset's CTEs - "no such table"."""
    author = await Author.objects.create(name="a")
    high = await Book.objects.create(name="high", author=author, rating=5)
    low = await Book.objects.create(name="low", author=author, rating=1)

    assert await _high_rated_books().contains(high) is True
    assert await _high_rated_books().contains(low) is False


@pytest.mark.asyncio
async def test_with_cte_bulk_update_keeps_cte(db):
    """bulk_update() used to render its UPDATE without the queryset's CTEs - "no such table"."""
    author = await Author.objects.create(name="a")
    high = await Book.objects.create(name="high", author=author, rating=5)
    low = await Book.objects.create(name="low", author=author, rating=1)
    high.name = "high renamed"
    low.name = "low renamed"

    await _high_rated_books().bulk_update([high, low], ["name"])

    assert sorted(await Book.objects.all().values_list("name", flat=True)) == ["high renamed", "low"]


@pytest.mark.asyncio
async def test_with_cte_union_of_branches_sharing_one_cte(db):
    """Two union() branches derived from one .with_cte() queryset define the same CTE - each
    branch compiles the body into its own query object, which used to be reported as a
    conflicting definition."""
    first_author = await Author.objects.create(name="first")
    second_author = await Author.objects.create(name="second")
    await Book.objects.create(name="first high", author=first_author, rating=5)
    await Book.objects.create(name="first low", author=first_author, rating=1)
    await Book.objects.create(name="second high", author=second_author, rating=4)

    base = _high_rated_books()
    shared = await base.filter(author=first_author).union(base.filter(author=second_author))
    separately_built = (
        await _high_rated_books().filter(author=first_author).union(_high_rated_books().filter(author=second_author))
    )

    assert sorted(book.name for book in shared) == ["first high", "second high"]
    assert sorted(book.name for book in separately_built) == ["first high", "second high"]


@pytest.mark.asyncio
async def test_with_cte_union_of_differently_defined_ctes_raises(db):
    """Same-named CTEs whose bodies differ are still rejected."""
    lenient = Book.objects.all().with_cte("picked", Book.objects.filter(rating__gte=1).values("id"))
    strict = Book.objects.all().with_cte("picked", Book.objects.filter(rating__gte=4).values("id"))

    with pytest.raises(QueryError, match="defined differently"):
        await lenient.union(strict)
