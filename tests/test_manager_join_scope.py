"""A JOIN to a related model must be scoped by that model's FULL default Manager scope (whatever
its ``Meta.manager.get_queryset()`` applies), not just ``Meta.tenant_field``/
``Meta.soft_delete_field`` - exactly like ``related_model.objects.all()`` itself."""

import pytest

from hare.exceptions import (
    ConfigurationError,
    QueryError,
)
from hare.models.tenancy import Tenancy
from hare.query.manager import Manager
from hare.query.scopes.row_scopes import RowScopes
from hare.sql import Table
from tests.testmodels import (
    ActiveAuthor,
    ActiveAuthorBook,
    ActiveAuthorTag,
    AnnotatedScopedBook,
    BareManagerTenantAuthor,
    BareManagerTenantAuthorBook,
    CrossRelationScopedBook,
    TenantActiveAuthor,
    TenantActiveAuthorBook,
    TenantActiveReview,
)


async def _create_authors_and_books():
    active = await ActiveAuthor.objects.create(name="Active", is_active=True)
    inactive = await ActiveAuthor.objects.create(name="Inactive", is_active=False)
    book_of_active = await ActiveAuthorBook.objects.create(title="B-active", author=active)
    book_of_inactive = await ActiveAuthorBook.objects.create(title="B-inactive", author=inactive)
    return active, inactive, book_of_active, book_of_inactive


@pytest.mark.asyncio
async def test_manager_hides_inactive_author_directly(db):
    """Baseline the JOIN paths below must match."""
    _, inactive, _, _ = await _create_authors_and_books()
    assert await ActiveAuthor.objects.filter(pk=inactive.pk) == []


@pytest.mark.asyncio
async def test_select_related_applies_custom_manager_filter(db):
    _, _, _, book_of_inactive = await _create_authors_and_books()
    books = {book.title: book for book in await ActiveAuthorBook.objects.all().select_related("author")}
    assert books["B-active"].author is not None
    assert books["B-active"].author.name == "Active"
    assert books["B-inactive"].author is None
    assert book_of_inactive.author_id is not None


@pytest.mark.asyncio
async def test_nested_filter_applies_custom_manager_filter(db):
    await _create_authors_and_books()
    assert await ActiveAuthorBook.objects.filter(author__name="Inactive") == []
    assert [book.title for book in await ActiveAuthorBook.objects.filter(author__name="Active")] == ["B-active"]


@pytest.mark.asyncio
async def test_nested_filter_with_select_related_applies_custom_manager_filter(db):
    await _create_authors_and_books()
    assert await ActiveAuthorBook.objects.filter(author__name="Inactive").select_related("author") == []


@pytest.mark.asyncio
async def test_only_across_relation_applies_custom_manager_filter(db):
    await _create_authors_and_books()
    books = {book.title: book for book in await ActiveAuthorBook.objects.all().only("title", "author__name")}
    assert books["B-active"].author.name == "Active"
    assert books["B-inactive"].author is None


@pytest.mark.asyncio
async def test_order_by_across_relation_applies_custom_manager_filter(db):
    """The JOIN built just for ordering must not multiply/alter rows either - and its ON condition
    is the same scoped one, so a row whose author is hidden sorts as if it had no author."""
    await _create_authors_and_books()
    titles = [book.title for book in await ActiveAuthorBook.objects.all().order_by("author__name", "title")]
    assert sorted(titles) == ["B-active", "B-inactive"]
    assert len(titles) == 2
    # NULLs (the hidden author) sort first on SQLite and last on Postgres, but never between.
    assert titles in (["B-inactive", "B-active"], ["B-active", "B-inactive"])
    ordered_by_hidden_author = await ActiveAuthorBook.objects.filter(author__name__isnull=True).values_list(
        "title", flat=True
    )
    assert ordered_by_hidden_author == ["B-inactive"]


@pytest.mark.asyncio
async def test_values_across_relation_applies_custom_manager_filter(db):
    await _create_authors_and_books()
    rows = await ActiveAuthorBook.objects.all().order_by("title").values_list("title", "author__name")
    assert rows == [("B-active", "Active"), ("B-inactive", None)]


@pytest.mark.asyncio
async def test_exclude_across_relation_still_scoped(db):
    await _create_authors_and_books()
    titles = {book.title for book in await ActiveAuthorBook.objects.exclude(author__name="Active")}
    # The hidden author's book has no visible author at all, so it is not "an Active author's book".
    assert titles == {"B-inactive"}


@pytest.mark.asyncio
async def test_m2m_related_model_custom_manager_filter(db):
    with Tenancy.scope(1):
        author = await TenantActiveAuthor.objects.create(name="A", company_id=1)
        book = await TenantActiveAuthorBook.objects.create(title="B", author=author)
        active_tag = await ActiveAuthorTag.objects.create(name="live", is_active=True)
        hidden_tag = await ActiveAuthorTag.objects.create(name="hidden", is_active=True)
        await book.tags.add(active_tag, hidden_tag)
        await ActiveAuthorTag.objects.filter(id=hidden_tag.id).update(is_active=False)
        assert await TenantActiveAuthorBook.objects.filter(tags__name="live") == [book]
        assert await TenantActiveAuthorBook.objects.filter(tags__name="hidden") == []


@pytest.mark.asyncio
async def test_custom_manager_combined_with_tenant_and_soft_delete(db):
    with Tenancy.scope(1):
        visible = await TenantActiveAuthor.objects.create(name="Visible", company_id=1)
        inactive = await TenantActiveAuthor.objects.create(name="Inactive", company_id=1, is_active=False)
        deleted = await TenantActiveAuthor.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
        for author in (visible, inactive, deleted):
            await TenantActiveAuthorBook.objects.create(title=author.name, author=author)
        with Tenancy.scope(2):
            other_tenant = await TenantActiveAuthor.objects.create(name="OtherTenant", company_id=2)
        with Tenancy.scope(None):
            # A cross-tenant link, written as trusted seed data.
            await TenantActiveAuthorBook.objects.create(title="OtherTenant", author_id=other_tenant.id)

        books = {book.title: book for book in await TenantActiveAuthorBook.objects.all().select_related("author")}
        assert books["Visible"].author is not None
        assert books["Inactive"].author is None
        assert books["Deleted"].author is None
        assert books["OtherTenant"].author is None


@pytest.mark.asyncio
async def test_bare_custom_manager_never_weakens_join_tenant_and_soft_delete_scope(db):
    with Tenancy.scope(1):
        visible = await BareManagerTenantAuthor.objects.create(name="Visible", company_id=1)
        deleted = await BareManagerTenantAuthor.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
        with Tenancy.scope(2):
            other_tenant = await BareManagerTenantAuthor.objects.create(name="OtherTenant", company_id=2)
        await BareManagerTenantAuthorBook.objects.create(title="Visible", author=visible)
        await BareManagerTenantAuthorBook.objects.create(title="Deleted", author_id=deleted.id)
        with Tenancy.scope(None):
            # A cross-tenant link, written as trusted seed data.
            await BareManagerTenantAuthorBook.objects.create(title="OtherTenant", author_id=other_tenant.id)

        books = {book.title: book for book in await BareManagerTenantAuthorBook.objects.all().select_related("author")}
        assert books["Visible"].author is not None
        assert books["Deleted"].author is None
        assert books["OtherTenant"].author is None
        assert await BareManagerTenantAuthorBook.objects.filter(author__name="OtherTenant") == []
        assert len(await BareManagerTenantAuthorBook.objects.filter(author__name="Visible")) == 1


@pytest.mark.asyncio
async def test_escape_hatches_propagate_but_custom_filter_stays(db):
    with Tenancy.scope(1):
        inactive = await TenantActiveAuthor.objects.create(name="Inactive", company_id=1, is_active=False)
        deleted = await TenantActiveAuthor.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
        await TenantActiveReview.objects.create(text="inactive-review", company_id=1, author=inactive)
        await TenantActiveReview.objects.create(text="deleted-review", company_id=1, author_id=deleted.id)
    with Tenancy.scope(2):
        other = await TenantActiveAuthor.objects.create(name="Other", company_id=2)
        await TenantActiveReview.objects.create(text="other-review", company_id=2, author=other)

    # No tenant active at all - the escape hatches on the OUTER query must keep working for a
    # related model whose manager is custom, exactly as for the default one.
    reviews = {
        review.text: review
        for review in await TenantActiveReview.objects.all_tenants().include_deleted().select_related("author")
    }
    assert reviews["other-review"].author is not None
    assert reviews["deleted-review"].author is not None
    # The custom is_active filter has no escape hatch - the inactive author stays hidden.
    assert reviews["inactive-review"].author is None

    # Only all_tenants(): the soft-deleted author is still hidden by the JOIN's soft-delete scope.
    with Tenancy.scope(1):
        reviews = {
            review.text: review for review in await TenantActiveReview.objects.all_tenants().select_related("author")
        }
    assert reviews["other-review"].author is not None
    assert reviews["deleted-review"].author is None


@pytest.mark.asyncio
async def test_no_active_tenant_raises_for_scoped_related_manager(db):
    with pytest.raises(QueryError):
        await TenantActiveAuthorBook.objects.all().select_related("author")


@pytest.mark.asyncio
async def test_join_scope_uses_the_tenant_active_when_awaited(db):
    with Tenancy.scope(1):
        author = await TenantActiveAuthor.objects.create(name="Mine", company_id=1)
        await TenantActiveAuthorBook.objects.create(title="B", author=author)
    queryset = TenantActiveAuthorBook.objects.all().select_related("author")
    with Tenancy.scope(1):
        books = await queryset
    assert books[0].author is not None
    assert books[0].author.name == "Mine"
    with Tenancy.scope(2):
        books = await queryset
    assert books[0].author is None


@pytest.mark.asyncio
async def test_cross_relation_default_scope_raises_clear_error(db):
    with pytest.raises(ConfigurationError, match="filters across a relation"):
        await CrossRelationScopedBook.objects.all().select_related("author")


@pytest.mark.asyncio
async def test_annotated_default_scope_raises_clear_error(db):
    with pytest.raises(ConfigurationError, match="annotate"):
        await AnnotatedScopedBook.objects.all().select_related("author")


@pytest.mark.asyncio
async def test_raw_criterion_includes_custom_manager_filter(db):
    criterion = RowScopes.of(ActiveAuthor).get_criterion(
        Table("activeauthor"), dialect=ActiveAuthor._meta.db.dialect, connection=None
    )
    assert criterion is not None
    table = Table("activeauthor")
    sql, _ = ActiveAuthor._meta.db.query_class.from_(table).select(table.id).where(criterion).get_parameterized_sql()
    assert "is_active" in sql


@pytest.mark.asyncio
async def test_raw_criterion_none_for_unscoped_default_manager(db):
    from tests.testmodels import Tournament

    assert (
        RowScopes.of(Tournament).get_criterion(
            Table("tournament"), dialect=Tournament._meta.db.dialect, connection=None
        )
        is None
    )


async def _create_tenant_scoped_reviews():
    """Tenant 1: a visible author, an inactive one, a soft-deleted one - each with one review. Tenant
    2: one more author/review, to prove tenant scoping still holds next to the custom filter."""
    with Tenancy.scope(1):
        visible = await TenantActiveAuthor.objects.create(name="Visible", company_id=1)
        inactive = await TenantActiveAuthor.objects.create(name="Inactive", company_id=1, is_active=False)
        deleted = await TenantActiveAuthor.objects.create(name="Deleted", company_id=1)
        await deleted.delete()
        await TenantActiveReview.objects.create(text="visible", company_id=1, author=visible)
        await TenantActiveReview.objects.create(text="inactive", company_id=1, author=inactive)
        await TenantActiveReview.objects.create(text="deleted", company_id=1, author_id=deleted.id)
    with Tenancy.scope(2):
        other = await TenantActiveAuthor.objects.create(name="Other", company_id=2)
        await TenantActiveReview.objects.create(text="other", company_id=2, author=other)


@pytest.mark.asyncio
async def test_nested_filter_tenant_soft_delete_and_custom_filter_all_apply(db):
    await _create_tenant_scoped_reviews()
    with Tenancy.scope(1):
        for hidden_name in ("Inactive", "Deleted", "Other"):
            assert await TenantActiveReview.objects.filter(author__name=hidden_name) == []
        assert [review.text for review in await TenantActiveReview.objects.filter(author__name="Visible")] == [
            "visible"
        ]


@pytest.mark.asyncio
async def test_include_deleted_alone_lifts_only_the_soft_delete_part_of_the_join_scope(db):
    await _create_tenant_scoped_reviews()
    with Tenancy.scope(1):
        reviews = {
            review.text: review
            for review in await TenantActiveReview.objects.include_deleted().select_related("author")
        }
        assert reviews["visible"].author is not None
        assert reviews["deleted"].author is not None
        assert reviews["inactive"].author is None
        assert [r.text for r in await TenantActiveReview.objects.include_deleted().filter(author__name="Deleted")] == [
            "deleted"
        ]
        assert await TenantActiveReview.objects.include_deleted().filter(author__name="Inactive") == []


@pytest.mark.asyncio
async def test_all_tenants_alone_lifts_only_the_tenant_part_of_the_join_scope(db):
    await _create_tenant_scoped_reviews()
    with Tenancy.scope(1):
        reviews = {
            review.text: review for review in await TenantActiveReview.objects.all_tenants().select_related("author")
        }
        assert reviews["other"].author is not None
        assert reviews["visible"].author is not None
        assert reviews["deleted"].author is None
        assert reviews["inactive"].author is None
        assert await TenantActiveReview.objects.all_tenants().filter(author__name="Inactive") == []
        assert [r.text for r in await TenantActiveReview.objects.all_tenants().filter(author__name="Other")] == [
            "other"
        ]


@pytest.mark.asyncio
async def test_order_by_and_values_across_relation_use_the_full_join_scope(db):
    await _create_tenant_scoped_reviews()
    with Tenancy.scope(1):
        rows = await TenantActiveReview.objects.all().order_by("text").values_list("text", "author__name")
    assert rows == [("deleted", None), ("inactive", None), ("visible", "Visible")]


@pytest.mark.asyncio
async def test_prefetch_and_select_related_agree_on_custom_manager_scope(db):
    """prefetch_related() already went through the related model's real get_queryset() - the JOIN
    paths must now hide exactly the same rows."""
    await _create_tenant_scoped_reviews()
    with Tenancy.scope(1):
        joined = {
            review.text: review.author is not None
            for review in await TenantActiveReview.objects.all().select_related("author")
        }
        prefetched = {
            review.text: review.author is not None
            for review in await TenantActiveReview.objects.all().prefetch_related("author")
        }
    assert joined == prefetched == {"visible": True, "inactive": False, "deleted": False}


@pytest.mark.asyncio
async def test_join_scope_override_is_always_reset(db):
    """The ContextVar published for the related model's get_queryset() call must never leak out -
    neither after a normal JOIN build nor after one that raises."""
    await _create_authors_and_books()
    assert RowScopes.requested_visibility.get() is None
    await ActiveAuthorBook.objects.all().select_related("author")
    assert RowScopes.requested_visibility.get() is None
    with pytest.raises(ConfigurationError):
        await AnnotatedScopedBook.objects.all().select_related("author")
    assert RowScopes.requested_visibility.get() is None
    # A plain query on the custom-manager model itself, after the JOIN, is unaffected.
    assert await ActiveAuthor.objects.filter(name="Inactive") == []


@pytest.mark.asyncio
async def test_default_manager_join_scope_unchanged_for_plain_tenant_model(db):
    """A model WITHOUT a custom get_queryset() keeps the exact pre-existing tenant + soft-delete
    JOIN scoping (no manager call involved)."""
    from tests.testmodels import TenantScopedFactory, TenantScopedWidget

    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="F", company_id=1)
        gone = await TenantScopedFactory.objects.create(name="Gone", company_id=1)
        await gone.delete()
        await TenantScopedWidget.objects.create(name="W1", company_id=1, factory=factory)
        await TenantScopedWidget.objects.create(name="W2", company_id=1, factory_id=gone.id)
        widgets = {w.name: w for w in await TenantScopedWidget.objects.all().select_related("factory")}
    assert widgets["W1"].factory is not None
    assert widgets["W2"].factory is None
    assert not Manager.has_custom_get_queryset(TenantScopedFactory)
    assert Manager.has_custom_get_queryset(ActiveAuthor)


@pytest.mark.asyncio
async def test_bare_backward_fk_lookups_hide_soft_deleted_related_rows(db):
    from tests.testmodels import SoftDeleteChildCascadeSoft, SoftDeleteParent

    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)
    await child.delete()

    assert await SoftDeleteParent.objects.filter(cascade_children__isnull=True) == [parent]
    assert await SoftDeleteParent.objects.filter(cascade_children__isnull=False) == []
    assert await SoftDeleteParent.objects.filter(cascade_children=child) == []
    assert await SoftDeleteParent.objects.filter(cascade_children__in=[child.pk]) == []
    assert await SoftDeleteParent.objects.exclude(cascade_children__isnull=False) == [parent]
    assert await SoftDeleteParent.objects.filter(cascade_children__isnull=False).include_deleted() == [parent]

    await child.restore()
    assert await SoftDeleteParent.objects.filter(cascade_children=child) == [parent]
    assert await SoftDeleteParent.objects.filter(cascade_children__isnull=True) == []


@pytest.mark.asyncio
async def test_bare_backward_o2o_lookups_hide_soft_deleted_related_row(db):
    from tests.testmodels import SoftDeleteChildO2O, SoftDeleteParent

    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildO2O.objects.create(name="C", parent=parent)
    await child.delete()

    assert await SoftDeleteParent.objects.filter(soft_o2o_child__isnull=True) == [parent]
    assert await SoftDeleteParent.objects.filter(soft_o2o_child__isnull=False) == []
    assert await SoftDeleteParent.objects.filter(soft_o2o_child=child.pk) == []
    assert await SoftDeleteParent.objects.filter(soft_o2o_child__isnull=False).include_deleted() == [parent]


@pytest.mark.asyncio
async def test_bare_backward_o2o_lookup_accepts_a_related_instance(db):
    from tests.testmodels import SoftDeleteChildO2O, SoftDeleteParent

    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteParent.objects.create(name="other")
    child = await SoftDeleteChildO2O.objects.create(name="C", parent=parent)

    assert await SoftDeleteParent.objects.filter(soft_o2o_child=child) == [parent]
    assert await SoftDeleteParent.objects.exclude(soft_o2o_child=child).values_list("name", flat=True) == ["other"]


@pytest.mark.asyncio
async def test_bare_backward_fk_lookups_apply_related_tenant_scope(db):
    with Tenancy.scope(1):
        author = await TenantActiveAuthor.objects.create(name="A", company_id=1)
    # No tenant active: a cross-tenant link is written as trusted seed data.
    foreign_review = await TenantActiveReview.objects.create(text="foreign", company_id=2, author_id=author.id)

    with Tenancy.scope(1):
        assert await TenantActiveAuthor.objects.filter(reviews__isnull=False) == []
        assert await TenantActiveAuthor.objects.filter(reviews=foreign_review) == []
        assert await TenantActiveAuthor.objects.filter(reviews__isnull=True) == [author]
        assert await TenantActiveAuthor.objects.filter(reviews=foreign_review).all_tenants() == [author]


@pytest.mark.asyncio
async def test_m2m_isnull_applies_the_related_models_custom_manager_scope(db):
    inactive_tag = await ActiveAuthorTag.objects.create(name="inactive", is_active=True)
    active_tag = await ActiveAuthorTag.objects.create(name="active")
    only_inactive = await TenantActiveAuthorBook.objects.create(title="only-inactive")
    with_active = await TenantActiveAuthorBook.objects.create(title="with-active")
    await TenantActiveAuthorBook.objects.create(title="untagged")
    await only_inactive.tags.add(inactive_tag)
    await with_active.tags.add(active_tag, inactive_tag)
    await ActiveAuthorTag.objects.filter(id=inactive_tag.id).update(is_active=False)

    untagged = await TenantActiveAuthorBook.objects.filter(tags__isnull=True).values_list("title", flat=True)
    assert sorted(untagged) == ["only-inactive", "untagged"]
    assert await TenantActiveAuthorBook.objects.filter(tags__isnull=False).values_list("title", flat=True) == [
        "with-active"
    ]


@pytest.mark.asyncio
async def test_forward_isnull_treats_a_target_the_manager_hides_as_no_target(db):
    """author__isnull=True missed a book whose author the custom manager hides, while reading the
    relation gives None for it."""
    _, _, book_of_active, book_of_inactive = await _create_authors_and_books()
    book_without_author = await ActiveAuthorBook.objects.create(title="B-none", author=None)

    assert (await ActiveAuthorBook.objects.get(pk=book_of_inactive.pk).select_related("author")).author is None
    assert set(await ActiveAuthorBook.objects.filter(author__isnull=True).values_list("id", flat=True)) == {
        book_of_inactive.pk,
        book_without_author.pk,
    }
    assert await ActiveAuthorBook.objects.filter(author__isnull=False).values_list("id", flat=True) == [
        book_of_active.pk
    ]


@pytest.mark.asyncio
async def test_nested_isnull_on_a_hidden_targets_column_agrees_with_forward_isnull(db):
    """A hidden target counts as no target, so its columns read as NULL - the same rows
    author__isnull matches, in filter() and exclude()."""
    _, _, book_of_active, book_of_inactive = await _create_authors_and_books()
    book_without_author = await ActiveAuthorBook.objects.create(title="B-none", author=None)
    without_visible_author = {book_of_inactive.pk, book_without_author.pk}
    for lookup in ("author__isnull", "author__name__isnull", "author__id__isnull"):
        assert set(await ActiveAuthorBook.objects.filter(**{lookup: True}).values_list("id", flat=True)) == (
            without_visible_author
        ), lookup
        assert await ActiveAuthorBook.objects.filter(**{lookup: False}).values_list("id", flat=True) == [
            book_of_active.pk
        ]
        assert await ActiveAuthorBook.objects.exclude(**{lookup: True}).values_list("id", flat=True) == [
            book_of_active.pk
        ]
    assert (
        set(await ActiveAuthorBook.objects.filter(author__name=None).values_list("id", flat=True))
        == without_visible_author
    )


@pytest.mark.asyncio
async def test_nested_isnull_treats_tenant_soft_delete_and_manager_hidden_targets_alike(db):
    await _create_tenant_scoped_reviews()
    with Tenancy.scope(2):
        other_author = await TenantActiveAuthor.objects.get(name="Other")
    # No tenant active: a cross-tenant link is written as trusted seed data.
    await TenantActiveReview.objects.create(text="cross-tenant", company_id=1, author_id=other_author.id)
    with Tenancy.scope(1):
        hidden_target_texts = {"inactive", "deleted", "cross-tenant"}
        for lookup in ("author__isnull", "author__name__isnull"):
            texts = await TenantActiveReview.objects.filter(**{lookup: True}).values_list("text", flat=True)
            assert set(texts) == hidden_target_texts, lookup
            assert await TenantActiveReview.objects.exclude(**{lookup: True}).values_list("text", flat=True) == [
                "visible"
            ]
