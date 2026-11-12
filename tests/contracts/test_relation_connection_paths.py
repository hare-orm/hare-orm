"""Relation shapes follow saved connections and runtime routing for single and composite keys."""

import os

import pytest
import pytest_asyncio

from hare import Prefetch, prefetch_related_objects
from hare.core.routing.written_connections import WrittenConnections
from tests.relations.connection_rebinding_models import Author, Book, Edition, Review
from tests.utils.multi_database_context import MultiDatabaseTestContext
from tests.utils.parent_connection_router import ParentConnectionRouter
from tests.utils.relation_query_checks import RelationQueryChecks


@pytest.fixture(
    params=[
        (Author, "books", (" 1", " 2")),
        (Author, "selected_books", (" 1", " 2")),
        (Author, "selected_editions", ("", " extra")),
        (Edition, "reviews", (" 1", " 2")),
        (Edition, "readers", ("",)),
    ],
    ids=["reverse-fk", "m2m", "m2m-composite-target", "reverse-composite-fk", "m2m-composite-source"],
)
def relation_case(request):
    return request.param


@pytest_asyncio.fixture
async def relation_databases():
    async with MultiDatabaseTestContext.open(
        os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        ["models", "replica"],
        apps={
            "models": {
                "models": ["tests.relations.connection_rebinding_models"],
                "default_connection": "models",
            }
        },
    ) as context:
        await context.generate_schemas()
        await context.connections.get("replica").execute_script(
            context.connections.get("models").get_schema_sql(safe=True)
        )
        for alias in ("models", "replica"):
            author = await Author.objects.using(alias).create(id=1, name=alias)
            edition = await Edition.objects.using(alias).create(number=1, revision=2, name=alias)
            extra_edition = await Edition.objects.using(alias).create(number=1, revision=3, name=alias + " extra")
            for number in (1, 2):
                book = await Book.objects.using(alias).create(id=number, name=f"{alias} {number}", author=author)
                await Review.objects.using(alias).create(id=number, name=f"{alias} {number}", edition=edition)
                await author.selected_books.add(book, using=alias)
            await author.selected_editions.add(edition, extra_edition, using=alias)
        yield context


@pytest_asyncio.fixture
async def routed_relation_databases(relation_databases):
    relation_databases.router.init_routers([ParentConnectionRouter])
    token = WrittenConnections.current.set(None)
    try:
        yield relation_databases
    finally:
        WrittenConnections.current.reset(token)
        relation_databases.router.init_routers([])


@pytest.mark.asyncio
@pytest.mark.usefixtures("relation_databases")
async def test_relation_queries_follow_a_parent_saved_on_another_connection(relation_case):
    model, relation_name, suffixes = relation_case
    key = (1, 2) if model is Edition else 1
    parent = await model.objects.get(pk=key)
    await RelationQueryChecks.check(getattr(parent, relation_name), ["models" + suffix for suffix in suffixes])
    pinned = getattr(parent, relation_name).all().using("models")
    for alias in ("replica", "models"):
        await parent.save(using=alias)
        relation = getattr(parent, relation_name)
        await RelationQueryChecks.check(relation, [alias + suffix for suffix in suffixes])
        await RelationQueryChecks.check(pinned, ["models" + suffix for suffix in suffixes])


@pytest.mark.asyncio
@pytest.mark.usefixtures("routed_relation_databases")
async def test_relation_queries_consult_the_parent_router_when_executed(relation_case):
    model, relation_name, suffixes = relation_case
    key = (1, 2) if model is Edition else 1
    parent = await model.objects.using("replica").get(pk=key)
    query = getattr(parent, relation_name).values_list("name", flat=True)
    replica_names = sorted("replica" + suffix for suffix in suffixes)
    model_names = sorted("models" + suffix for suffix in suffixes)
    assert sorted(await query) == replica_names
    matching = getattr(parent, relation_name).filter(name__startswith="models")
    combined = query.union(query.none())
    pinned = query.using("replica")
    token = ParentConnectionRouter.routed_models.set(frozenset({model}))
    try:
        assert sorted(await query) == model_names
        assert await matching.count() == len(model_names)
        assert await matching.exists()
        assert sorted(await combined) == model_names
        relation = getattr(parent, relation_name)
        await RelationQueryChecks.check(relation, model_names)
        assert sorted(await pinned) == replica_names
        related = relation.model.objects.order_by("name")
        await prefetch_related_objects(
            [parent], Prefetch(relation_name, queryset=related[:1], to_attribute="first_related")
        )
        assert [obj.name for obj in parent.first_related] == model_names[:1]
        await prefetch_related_objects(
            [parent], Prefetch(relation_name, queryset=related.using("replica"), to_attribute="replica_related")
        )
        assert [obj.name for obj in parent.replica_related] == replica_names
    finally:
        ParentConnectionRouter.routed_models.reset(token)
    assert sorted(await query) == replica_names
