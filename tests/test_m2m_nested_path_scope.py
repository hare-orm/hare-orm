"""A nested many-to-many path (``relation__field``) only reaches links to rows the related model's
default scope (tenant, soft delete, ``Meta.manager``) shows - a link to a hidden row neither
matches ``__isnull=True`` nor adds a NULL row to values()/order_by()/annotate()."""

import pytest
import pytest_asyncio

from hare.models.tenancy import Tenancy
from hare.query.expressions import F
from hare.query.functions import Count
from tests.testmodels import (
    ActiveAuthorTag,
    SharedCollection,
    SharedCollectionEntry,
    SharedTopic,
    TenantActiveAuthorBook,
    TenantArticle,
    TenantWikiPage,
)


@pytest_asyncio.fixture
async def topics(db) -> None:
    shared_topic = await SharedTopic.objects.create(id=1, name="shared")
    other_tenant_only_topic = await SharedTopic.objects.create(id=2, name="other-tenant-only")
    deleted_only_topic = await SharedTopic.objects.create(id=3, name="deleted-only")
    await SharedTopic.objects.create(id=4, name="unlinked")
    with Tenancy.scope(1):
        own_article = await TenantArticle.objects.create(id=1, title="own")
        deleted_article = await TenantArticle.objects.create(id=3, title="deleted")
        await shared_topic.articles.add(own_article)
        await deleted_only_topic.articles.add(deleted_article)
        await deleted_article.delete()
    with Tenancy.scope(2):
        other_tenant_article = await TenantArticle.objects.create(id=2, title="other-tenant")
        await shared_topic.articles.add(other_tenant_article)
        await other_tenant_only_topic.articles.add(other_tenant_article)


UNLINKED_IN_TENANT_1 = ["deleted-only", "other-tenant-only", "unlinked"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "negate", "expected_names"),
    [
        ({"articles__id__isnull": True}, False, UNLINKED_IN_TENANT_1),
        ({"articles__title__isnull": True}, False, UNLINKED_IN_TENANT_1),
        ({"articles__id__isnull": False}, True, UNLINKED_IN_TENANT_1),
        ({"articles__id__isnull": False}, False, ["shared"]),
        ({"articles__title": "own"}, False, ["shared"]),
        ({"articles__title": "other-tenant"}, False, []),
        ({"articles__title": "own"}, True, UNLINKED_IN_TENANT_1),
    ],
)
async def test_nested_m2m_filter_ignores_links_to_hidden_rows(topics, lookup, negate, expected_names):
    with Tenancy.scope(1):
        queryset = SharedTopic.objects.exclude(**lookup) if negate else SharedTopic.objects.filter(**lookup)
        assert sorted(await queryset.values_list("name", flat=True)) == expected_names


@pytest.mark.asyncio
async def test_nested_m2m_isnull_cleanup_keeps_a_row_the_tenant_still_uses(topics):
    with Tenancy.scope(1):
        assert await SharedTopic.objects.filter(articles__id__isnull=True).delete() == 3
        assert await SharedTopic.objects.all().values_list("name", flat=True) == ["shared"]
        assert await TenantArticle.objects.filter(id=1).values_list("topics__name", flat=True) == ["shared"]


@pytest.mark.asyncio
async def test_nested_m2m_values_order_by_and_annotate_add_no_rows_for_hidden_links(topics):
    with Tenancy.scope(1):
        assert await SharedTopic.objects.all().order_by("id").values_list("name", "articles__id") == [
            ("shared", 1),
            ("other-tenant-only", None),
            ("deleted-only", None),
            ("unlinked", None),
        ]
        assert len(await SharedTopic.objects.all().order_by("articles__title")) == 4
        assert await SharedTopic.objects.all().order_by("articles__title").count() == 4
        assert await SharedTopic.objects.annotate(article_title=F("articles__title")).order_by("id").values_list(
            "name", "article_title"
        ) == [("shared", "own"), ("other-tenant-only", None), ("deleted-only", None), ("unlinked", None)]
        assert await SharedTopic.objects.annotate(article_count=Count("articles")).order_by("id").values_list(
            "article_count", flat=True
        ) == [1, 0, 0, 0]
        assert await SharedTopic.objects.all().aggregate(article_count=Count("articles__id")) == {"article_count": 1}


@pytest.mark.asyncio
async def test_nested_m2m_reverse_side_to_an_unscoped_target_is_unchanged(topics):
    with Tenancy.scope(2):
        assert await TenantArticle.objects.filter(topics__name="shared").values_list("id", flat=True) == [2]
        assert await TenantArticle.objects.all().order_by("topics__id").values_list("id", "topics__id") == [
            (2, 1),
            (2, 2),
        ]


@pytest.mark.asyncio
async def test_nested_m2m_custom_through_honours_both_through_and_target_scope(topics):
    visible_collection = await SharedCollection.objects.create(id=1, name="visible")
    hidden_collection = await SharedCollection.objects.create(id=2, name="hidden")
    await SharedCollectionEntry.objects.create(collection=visible_collection, article_id=1)
    await SharedCollectionEntry.objects.create(collection=visible_collection, article_id=2)
    await SharedCollectionEntry.objects.create(collection=hidden_collection, article_id=2)
    removed_entry = await SharedCollectionEntry.objects.create(collection=hidden_collection, article_id=1)
    await removed_entry.delete()
    with Tenancy.scope(1):
        assert await SharedCollection.objects.filter(articles__id__isnull=True).values_list("name", flat=True) == [
            "hidden"
        ]
        assert await SharedCollection.objects.all().order_by("id").values_list("name", "articles__title") == [
            ("visible", "own"),
            ("hidden", None),
        ]
        assert await TenantArticle.objects.filter(collections__name="visible").values_list("id", flat=True) == [1]


@pytest_asyncio.fixture
async def wiki_pages(db) -> None:
    with Tenancy.scope(1):
        await TenantWikiPage.objects.create(id=1, title="home", company_id=1)
        await TenantWikiPage.objects.create(id=3, title="faq", company_id=1)
        deleted_page = await TenantWikiPage.objects.create(id=5, title="old", company_id=1)
        await deleted_page.delete()
    with Tenancy.scope(2):
        await TenantWikiPage.objects.create(id=2, title="other-home", company_id=2)
        await TenantWikiPage.objects.create(id=4, title="other-faq", company_id=2)
    see_also_field = TenantWikiPage._meta.fields_map["see_also"]
    connection = TenantWikiPage._meta.db
    for page_id, see_also_id in ((1, 2), (1, 3), (3, 5), (4, 1)):
        await connection.execute(
            f'INSERT INTO "{see_also_field.through}" '
            f'("{see_also_field.backward_key}", "{see_also_field.forward_key}") '
            f"VALUES ({page_id}, {see_also_id})"
        )


@pytest.mark.asyncio
async def test_nested_self_m2m_ignores_links_to_hidden_rows_on_both_sides(wiki_pages):
    with Tenancy.scope(1):
        assert await TenantWikiPage.objects.filter(see_also__id__isnull=True).values_list("title", flat=True) == [
            "faq"
        ]
        assert await TenantWikiPage.objects.all().order_by("id").values_list("id", "see_also__id") == [
            (1, 3),
            (3, None),
        ]
        assert await TenantWikiPage.objects.filter(referenced_by__id__isnull=True).values_list("title", flat=True) == [
            "home"
        ]
        assert await TenantWikiPage.objects.all().order_by("id").values_list("id", "referenced_by__id") == [
            (1, None),
            (3, 1),
        ]


@pytest.mark.asyncio
async def test_nested_self_m2m_escape_hatches_reach_hidden_links(wiki_pages):
    assert await TenantWikiPage.objects.all_tenants().filter(referenced_by__id__isnull=True).values_list(
        "id", flat=True
    ) == [4]
    with Tenancy.scope(1):
        assert await TenantWikiPage.objects.include_deleted().filter(see_also__title="old").values_list(
            "id", flat=True
        ) == [3]
        assert await TenantWikiPage.objects.filter(see_also__title="old").values_list("id", flat=True) == []


@pytest.mark.asyncio
async def test_nested_m2m_ignores_links_to_manager_hidden_rows(db):
    book = await TenantActiveAuthorBook.objects.create(title="book")
    active_tag = await ActiveAuthorTag.objects.create(name="active", is_active=True)
    inactive_tag = await ActiveAuthorTag.objects.create(name="inactive", is_active=False)
    await book.tags.add(active_tag)
    tags_field = TenantActiveAuthorBook._meta.fields_map["tags"]
    await TenantActiveAuthorBook._meta.db.execute(
        f'INSERT INTO "{tags_field.through}" ("{tags_field.backward_key}", "{tags_field.forward_key}") '
        f"VALUES ({book.pk}, {inactive_tag.pk})"
    )
    assert await TenantActiveAuthorBook.objects.filter(tags__id__isnull=True).count() == 0
    assert await TenantActiveAuthorBook.objects.all().values_list("tags__name", flat=True) == ["active"]
