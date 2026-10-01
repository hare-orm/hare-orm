"""ManyToManyRelation.add() checks against the database that the owner isn't soft-deleted and that
every added row is shown by the related model's default scope (soft delete, tenant,
``Meta.manager``) - a stale or hand-built instance doesn't get a through row to a hidden row."""

import pytest

from hare.exceptions import IntegrityError
from hare.models.tenancy import Tenancy
from tests.testmodels import ActiveAuthorTag, SharedTopic, TenantActiveAuthorBook, TenantArticle


async def get_through_row_count(relation_field) -> int:
    connection = relation_field.model._meta.db
    rows = await connection.execute_dicts(f'SELECT * FROM "{relation_field.through}"')
    return len(rows)


@pytest.mark.asyncio
async def test_add_rejects_a_stale_instance_soft_deleted_in_the_database(db):
    topic = await SharedTopic.objects.create(id=1, name="topic")
    with Tenancy.scope(1):
        await TenantArticle.objects.create(id=1, title="article")
        stale_article = await TenantArticle.objects.get(id=1)
        await TenantArticle.objects.filter(id=1).delete()
        assert stale_article.deleted_at is None
        with pytest.raises(IntegrityError, match="is not shown by TenantArticle's default scope"):
            await topic.articles.add(stale_article)
        forged_article = TenantArticle(id=1, title="article", company_id=1)
        forged_article._saved_in_db = True
        with pytest.raises(IntegrityError, match="is not shown by TenantArticle's default scope"):
            await topic.articles.add(forged_article)
    assert await get_through_row_count(SharedTopic._meta.fields_map["articles"]) == 0


@pytest.mark.asyncio
async def test_add_rejects_a_stale_owner_soft_deleted_in_the_database(db):
    topic = await SharedTopic.objects.create(id=1, name="topic")
    with Tenancy.scope(1):
        await TenantArticle.objects.create(id=1, title="article")
        stale_article = await TenantArticle.objects.get(id=1)
        await TenantArticle.objects.filter(id=1).delete()
        with pytest.raises(IntegrityError, match="is soft-deleted - can't add a relation from it"):
            await stale_article.topics.add(topic)
    assert await get_through_row_count(SharedTopic._meta.fields_map["articles"]) == 0


@pytest.mark.asyncio
async def test_add_rejects_a_row_hidden_by_the_related_manager(db):
    book = await TenantActiveAuthorBook.objects.create(title="book")
    active_tag = await ActiveAuthorTag.objects.create(name="active", is_active=True)
    inactive_tag = await ActiveAuthorTag.objects.create(name="inactive", is_active=False)
    with pytest.raises(IntegrityError, match="is not shown by ActiveAuthorTag's default scope"):
        await book.tags.add(active_tag, inactive_tag)
    assert await get_through_row_count(TenantActiveAuthorBook._meta.fields_map["tags"]) == 0
    await book.tags.add(active_tag)
    assert await book.tags.all().values_list("name", flat=True) == ["active"]


@pytest.mark.asyncio
async def test_remove_drops_a_link_to_a_hidden_row_that_clear_keeps(db):
    book = await TenantActiveAuthorBook.objects.create(title="book")
    first_tag = await ActiveAuthorTag.objects.create(name="first", is_active=True)
    second_tag = await ActiveAuthorTag.objects.create(name="second", is_active=True)
    await book.tags.add(first_tag, second_tag)
    await ActiveAuthorTag._meta.db.execute(
        f'UPDATE "{ActiveAuthorTag._meta.db_table}" SET "is_active" = FALSE WHERE "id" IN '
        f"({first_tag.pk}, {second_tag.pk})"
    )
    tags_field = TenantActiveAuthorBook._meta.fields_map["tags"]
    await book.tags.clear()
    assert await get_through_row_count(tags_field) == 2
    await book.tags.remove(first_tag)
    assert await get_through_row_count(tags_field) == 1
