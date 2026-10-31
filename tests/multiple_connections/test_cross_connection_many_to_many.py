"""A many-to-many relation to a model on another connection (``db_constraint=False``): its rows are
read by the keys its through table links them by, and the model declaring it filters by its links -
no query joins the two connections' tables."""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.exceptions import QueryError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model
from hare.models.tenancy.tenancy import Tenancy
from hare.query.relation_loading import Prefetch
from tests.utils.multi_database_context import MultiDatabaseTestContext

OWNER_MODULE_NAME = "tests._cross_conn_m2m_owner_models"
TARGET_MODULE_NAME = "tests._cross_conn_m2m_target_models"


@pytest_asyncio.fixture
async def cross_conn_m2m():
    class CrossConnLabel(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        deleted_at = fields.DatetimeField(null=True)

        class Meta:
            app = "ccm_target"
            soft_delete_field = "deleted_at"

    class CrossConnBadge(Model):
        series = fields.IntField()
        number = fields.IntField()
        pk = CompositePrimaryKey("series", "number")

        class Meta:
            app = "ccm_target"

    class CrossConnShelf(Model):
        id = fields.IntField(primary_key=True)
        store = fields.CharField(max_length=10)

        class Meta:
            app = "ccm_target"
            tenant_field = "store"

    class CrossConnArticle(Model):
        id = fields.IntField(primary_key=True)
        labels: fields.ManyToManyRelation[CrossConnLabel] = fields.ManyToManyField(
            "ccm_target.CrossConnLabel", related_name="articles", db_constraint=False
        )
        badges: fields.ManyToManyRelation[CrossConnBadge] = fields.ManyToManyField(
            "ccm_target.CrossConnBadge", related_name="articles", db_constraint=False
        )
        shelves: fields.ManyToManyRelation[CrossConnShelf] = fields.ManyToManyField(
            "ccm_target.CrossConnShelf", related_name="articles", db_constraint=False
        )

        class Meta:
            app = "ccm_owner"

    owner_module = types.ModuleType(OWNER_MODULE_NAME)
    setattr(owner_module, "CrossConnArticle", CrossConnArticle)  # noqa: B010
    target_module = types.ModuleType(TARGET_MODULE_NAME)
    setattr(target_module, "CrossConnLabel", CrossConnLabel)  # noqa: B010
    setattr(target_module, "CrossConnBadge", CrossConnBadge)  # noqa: B010
    setattr(target_module, "CrossConnShelf", CrossConnShelf)  # noqa: B010
    sys.modules[OWNER_MODULE_NAME] = owner_module
    sys.modules[TARGET_MODULE_NAME] = target_module

    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["ccm_owner", "ccm_target"],
            apps={
                "ccm_owner": {"models": [OWNER_MODULE_NAME], "default_connection": "ccm_owner"},
                "ccm_target": {"models": [TARGET_MODULE_NAME], "default_connection": "ccm_target"},
            },
        ) as ctx:
            await ctx.generate_schemas()
            yield CrossConnArticle, CrossConnLabel
    finally:
        sys.modules.pop(OWNER_MODULE_NAME, None)
        sys.modules.pop(TARGET_MODULE_NAME, None)


@pytest.mark.asyncio
async def test_the_relation_reads_its_rows_by_the_linked_keys(cross_conn_m2m):
    """Every read of the relation raised QueryError - its query joined the through table of the
    other connection."""
    CrossConnArticle, CrossConnLabel = cross_conn_m2m
    article = await CrossConnArticle.objects.create(id=1)
    other_article = await CrossConnArticle.objects.create(id=2)
    labels = [await CrossConnLabel.objects.create(id=label_id, name=f"label {label_id}") for label_id in (1, 2, 3)]
    await article.labels.add(labels[0], labels[1])
    await other_article.labels.add(labels[2])

    assert [label.id for label in await article.labels.all().order_by("id")] == [1, 2]
    assert await article.labels.count() == 2
    assert await article.labels.exists()
    assert await article.labels.filter(name="label 2").values_list("id", flat=True) == [2]
    assert (await article.labels.order_by("-id").first()).id == 2
    assert (await article.labels.get(name="label 1")).id == 1
    assert [label.id async for label in article.labels.order_by("id")] == [1, 2]
    # The related model's own scope applies - a soft-deleted label isn't read.
    await labels[0].delete()
    assert [label.id for label in await article.labels.all()] == [2]


@pytest.mark.asyncio
async def test_the_declaring_model_filters_by_the_relation_links(cross_conn_m2m):
    """A filter on the relation joined the related table of the other connection."""
    CrossConnArticle, CrossConnLabel = cross_conn_m2m
    article = await CrossConnArticle.objects.create(id=1)
    await CrossConnArticle.objects.create(id=2)
    label = await CrossConnLabel.objects.create(id=1, name="label")
    await article.labels.add(label)

    assert await CrossConnArticle.objects.filter(labels=label).values_list("id", flat=True) == [1]
    assert await CrossConnArticle.objects.filter(labels__in=[1]).values_list("id", flat=True) == [1]
    assert await CrossConnArticle.objects.filter(labels__isnull=True).values_list("id", flat=True) == [2]
    assert await CrossConnArticle.objects.filter(labels__id=1).values_list("id", flat=True) == [1]
    assert await CrossConnArticle.objects.filter(labels__pk__in=[1]).values_list("id", flat=True) == [1]
    assert [obj.id for obj in await label.articles.all()] == [1]
    # A filter needing a related row's field still can't cross the connections.
    with pytest.raises(QueryError, match="different database connection"):
        await CrossConnArticle.objects.filter(labels__name="label")


@pytest.mark.asyncio
async def test_clear_and_set_keep_the_links_to_rows_the_related_scope_hides(cross_conn_m2m):
    """add() read the related table on the through table's connection, and clear()/set() matched
    the visible rows with a subquery of it there."""
    CrossConnArticle, CrossConnLabel = cross_conn_m2m
    article = await CrossConnArticle.objects.create(id=1)
    hidden, shown, added = [
        await CrossConnLabel.objects.create(id=label_id, name=f"label {label_id}") for label_id in (1, 2, 3)
    ]
    await article.labels.add(hidden, shown)
    await hidden.delete()
    await article.labels.set([added])
    assert [label.id for label in await article.labels.all()] == [3]
    await article.labels.clear()
    assert await article.labels.count() == 0
    await hidden.restore()
    # The link to the row hidden while the relation was set and cleared is still there.
    assert [label.id for label in await article.labels.all()] == [1]


@pytest.mark.asyncio
async def test_writes_through_the_relation_read_each_model_on_its_own_connection(cross_conn_m2m):
    """create(), get_or_create(), update_or_create() and set() of keys read and wrote the related
    table on the through table's connection; slicing the relation filtered a sliced query."""
    CrossConnArticle, CrossConnLabel = cross_conn_m2m
    article = await CrossConnArticle.objects.create(id=1)
    created = await article.labels.create(id=1, name="created")
    fetched, was_created = await article.labels.get_or_create(name="created")
    assert (fetched.id, was_created) == (created.id, False)
    _, was_created = await article.labels.get_or_create(id=2, name="second")
    assert was_created
    updated, was_created = await article.labels.update_or_create({"name": "renamed"}, id=2)
    assert (updated.name, was_created) == ("renamed", False)
    await CrossConnLabel.objects.create(id=3, name="third")
    await article.labels.set([1, 3])
    assert [label.id for label in await article.labels.order_by("id")] == [1, 3]
    assert [label.id for label in await article.labels.order_by("id")[1:2]] == [3]
    assert [label.id for label in await article.labels.order_by("-id").limit(1)] == [3]
    # The relation's reverse side, of an owner with a soft delete field.
    await created.articles.add(await CrossConnArticle.objects.create(id=2))
    assert [obj.id for obj in await created.articles.order_by("id")] == [1, 2]


@pytest.mark.asyncio
async def test_a_slice_of_each_owners_rows(cross_conn_m2m):
    """The keys of each owner's slice were read joined to the through table of the other connection -
    refused: "points at CrossConnArticle, which resolves to a different database connection"."""
    CrossConnArticle, CrossConnLabel = cross_conn_m2m
    target_models = sys.modules[TARGET_MODULE_NAME]
    articles = [await CrossConnArticle.objects.create(id=article_id) for article_id in (1, 2, 3)]
    labels = [await CrossConnLabel.objects.create(id=label_id, name=f"label {label_id}") for label_id in (1, 2, 3, 4)]
    await articles[0].labels.add(*labels)
    await articles[1].labels.add(labels[0], labels[2])
    badges = [await target_models.CrossConnBadge.objects.create(series=1, number=number) for number in (1, 2, 3)]
    await articles[0].badges.add(*badges)
    await articles[1].badges.add(badges[1])
    # A soft-deleted label is no row of the slice.
    await labels[3].delete()

    async def get_label_ids(queryset):
        rows = await CrossConnArticle.objects.order_by("id").prefetch_related(
            Prefetch("labels", queryset=queryset, to_attribute="picked")
        )
        return [[label.id for label in article.picked] for article in rows]

    by_id_down = CrossConnLabel.objects.order_by("-id")
    assert await get_label_ids(by_id_down) == [[3, 2, 1], [3, 1], []]
    assert await get_label_ids(by_id_down[:1]) == [[3], [3], []]
    assert await get_label_ids(by_id_down[1:]) == [[2, 1], [1], []]
    assert await get_label_ids(by_id_down[1:2]) == [[2], [1], []]
    assert await get_label_ids(by_id_down[5:]) == [[], [], []]
    partial = await CrossConnArticle.objects.order_by("id").prefetch_related(
        Prefetch("labels", queryset=CrossConnLabel.objects.only("id").order_by("id")[:1])
    )
    assert [[(label.id, label._partial) for label in article.labels] for article in partial] == [
        [(1, True)],
        [(1, True)],
        [],
    ]
    rows = await CrossConnArticle.objects.order_by("id").prefetch_related(
        Prefetch("badges", queryset=target_models.CrossConnBadge.objects.order_by("-number")[:2])
    )
    assert [[badge.pk for badge in article.badges] for article in rows] == [[(1, 3), (1, 2)], [(1, 2)], []]
    # From the related side the through table is on the related rows' connection.
    rows = await CrossConnLabel.objects.order_by("id").prefetch_related(
        Prefetch("articles", queryset=CrossConnArticle.objects.order_by("-id")[:1])
    )
    assert [[article.id for article in label.articles] for label in rows] == [[2], [1], [2]]


@pytest.mark.asyncio
async def test_a_composite_key_and_a_tenant_field_are_read_on_the_related_connection(cross_conn_m2m):
    """A filter on a relation to a composite key joined its table; the tenant of a related row was
    read on the through table's connection."""
    CrossConnArticle, _ = cross_conn_m2m
    target_models = sys.modules[TARGET_MODULE_NAME]
    article = await CrossConnArticle.objects.create(id=1)
    await CrossConnArticle.objects.create(id=2)
    badge = await target_models.CrossConnBadge.objects.create(series=1, number=2)
    await article.badges.add(badge)
    assert await CrossConnArticle.objects.filter(badges=badge).values_list("id", flat=True) == [1]
    assert await CrossConnArticle.objects.exclude(badges=badge).values_list("id", flat=True) == [2]
    assert [obj.id for obj in await badge.articles.all()] == [1]
    with Tenancy.scope("north"):
        shelf = await target_models.CrossConnShelf.objects.create(id=1, store="north")
        await article.shelves.add(shelf)
        assert [obj.id for obj in await article.shelves.all()] == [1]
        await article.shelves.remove(shelf)
        assert await article.shelves.count() == 0


@pytest.mark.asyncio
async def test_a_query_of_the_other_connection_is_refused_with_a_query_error(cross_conn_m2m):
    """A subquery of the related model ran inside the other connection's statement - the driver's
    "no such table"; a set operation of the relation's querysets raised AttributeError."""
    CrossConnArticle, CrossConnLabel = cross_conn_m2m
    article = await CrossConnArticle.objects.create(id=1)
    with pytest.raises(QueryError, match="another connection"):
        await CrossConnArticle.objects.filter(labels__in=CrossConnLabel.objects.filter(name="x"))
    with pytest.raises(QueryError, match="another connection"):
        article.labels.filter(id=1).union(article.labels.filter(id=2))
