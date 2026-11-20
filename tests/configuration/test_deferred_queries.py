"""Queries built before Hare.init() has set their model up record their calls; the calls are
replayed when init finishes, so a queryset declared at module level keeps working, and every
invalid one is reported by init with the place it was built at."""

import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.core.hare_context import HareContext
from hare.exceptions import FieldError
from hare.models import Model
from hare.query.expressions import OuterReference, Q, Subquery
from hare.query.functions import Count
from hare.query.queryset import QuerySet
from hare.query.relation_loading.prefetching.prefetch import Prefetch

pytestmark = pytest.mark.sqlite_only

MODULE_NAME = "tests._deferred_query_models"


class DeferredAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=40)
    mentor = fields.ForeignKeyField("deferred_queries.DeferredAuthor", related_name="mentees", null=True)

    class Meta:
        app = "deferred_queries"
        table = "deferred_author"


class DeferredBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=40)
    author: fields.ForeignKeyRelation[DeferredAuthor] = fields.ForeignKeyField(
        "deferred_queries.DeferredAuthor", related_name="books"
    )

    class Meta:
        app = "deferred_queries"
        table = "deferred_book"


QUERIES_BUILT_BEFORE_INIT = {
    "books by author name": lambda: DeferredBook.objects.filter(author__name="Ann").order_by("author__name", "title"),
    "authors by book title": lambda: DeferredAuthor.objects.filter(books__title="B1").distinct(),
    "book titles with author": lambda: DeferredBook.objects.all().order_by("id").values("title", "author__name"),
    "author book keys": lambda: DeferredAuthor.objects.all().order_by("id").values_list("books", flat=True),
    "books with author": lambda: DeferredBook.objects.all().select_related("author").order_by("id"),
    "authors with books": lambda: DeferredAuthor.objects.all().prefetch_related(
        Prefetch("books", queryset=DeferredBook.objects.filter(title="B1"))
    ),
    "authors with book count": lambda: DeferredAuthor.objects.all().annotate(book_count=Count("books")).order_by("id"),
    "first book title": lambda: (
        DeferredAuthor.objects.all()
        .annotate(
            first_title=Subquery(
                DeferredBook.objects.filter(author_id=OuterReference("id")).order_by("title").values("title")[:1]
            )
        )
        .order_by("id")
    ),
    "book count": lambda: DeferredBook.objects.filter(author__name="Ann").count(),
    "union": lambda: DeferredBook.objects.filter(title="B1").union(DeferredBook.objects.filter(author__name="Bob")),
    "recursive authors": lambda: DeferredAuthor.objects.filter(name="Ann").with_recursive("mentees").order_by("name"),
}
MODULE_LEVEL_QUERIES = {name: build() for name, build in QUERIES_BUILT_BEFORE_INIT.items()}
MODULE_LEVEL_MODEL_SHORTCUTS = {
    "get": DeferredBook.objects.get(title="B2"),
    "get with Q": DeferredBook.objects.get(Q(title="B3")),
    "get_or_none": DeferredBook.objects.get(does_not_exist_exception=None, title="missing"),
    "exists": DeferredBook.objects.filter(author__name="Bob").exists(),
}


def get_context_config():
    module = types.ModuleType(MODULE_NAME)
    module.DeferredAuthor = DeferredAuthor  # type: ignore[attr-defined]
    module.DeferredBook = DeferredBook  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    return {
        "connections": {"default": "sqlite+aiosqlite://:memory:"},
        "apps": {"deferred_queries": {"models": [MODULE_NAME], "default_connection": "default"}},
    }


@pytest_asyncio.fixture
async def deferred_context():
    context = HareContext()
    await context.__aenter__()
    try:
        await context.init(config=get_context_config())
        await context.generate_schemas()
        ann = await DeferredAuthor.objects.create(id=1, name="Ann")
        bob = await DeferredAuthor.objects.create(id=2, name="Bob", mentor=ann)
        await DeferredBook.objects.create(id=1, title="B1", author=ann)
        await DeferredBook.objects.create(id=2, title="B2", author=ann)
        await DeferredBook.objects.create(id=3, title="B3", author=bob)
        yield context
    finally:
        await context.connections.close_all(discard=True)
        await context.__aexit__(None, None, None)
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.database_independent
def test_queries_built_before_init_have_their_final_class():
    assert type(MODULE_LEVEL_QUERIES["book titles with author"]) is QuerySet
    assert type(MODULE_LEVEL_QUERIES["author book keys"]) is QuerySet
    assert type(MODULE_LEVEL_QUERIES["books with author"]) is QuerySet


@pytest.mark.asyncio
async def test_queries_built_before_init_run_like_queries_built_after(deferred_context):
    for name, query in MODULE_LEVEL_QUERIES.items():
        query_built_after_init = QUERIES_BUILT_BEFORE_INIT[name]()
        assert query.sql() == query_built_after_init.sql(), name
    assert [book.title for book in await MODULE_LEVEL_QUERIES["books by author name"]] == ["B1", "B2"]
    assert [author.name for author in await MODULE_LEVEL_QUERIES["authors by book title"]] == ["Ann"]
    assert await MODULE_LEVEL_QUERIES["book titles with author"] == [
        {"title": "B1", "author__name": "Ann"},
        {"title": "B2", "author__name": "Ann"},
        {"title": "B3", "author__name": "Bob"},
    ]
    assert sorted(await MODULE_LEVEL_QUERIES["author book keys"]) == [1, 2, 3]
    assert [book.author.name for book in await MODULE_LEVEL_QUERIES["books with author"]] == ["Ann", "Ann", "Bob"]
    authors = sorted(await MODULE_LEVEL_QUERIES["authors with books"], key=lambda author: author.id)
    assert [[book.title for book in author.books] for author in authors] == [["B1"], []]
    counted = await MODULE_LEVEL_QUERIES["authors with book count"]
    assert [author.book_count for author in counted] == [2, 1]
    first_titles = await MODULE_LEVEL_QUERIES["first book title"]
    assert [author.first_title for author in first_titles] == ["B1", "B3"]
    assert await MODULE_LEVEL_QUERIES["book count"] == 2
    assert sorted(book.title for book in await MODULE_LEVEL_QUERIES["union"]) == ["B1", "B3"]
    assert [author.name for author in await MODULE_LEVEL_QUERIES["recursive authors"]] == ["Ann", "Bob"]


@pytest.mark.asyncio
async def test_model_shortcuts_built_before_init(deferred_context):
    """Model.objects.get() / get_or_none() / exists() built before init are recorded like querysets."""
    assert (await MODULE_LEVEL_MODEL_SHORTCUTS["get"]).title == "B2"
    assert (await MODULE_LEVEL_MODEL_SHORTCUTS["get with Q"]).title == "B3"
    assert await MODULE_LEVEL_MODEL_SHORTCUTS["get_or_none"] is None
    assert await MODULE_LEVEL_MODEL_SHORTCUTS["exists"] is True


class DeferredErrorAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=40)

    class Meta:
        app = "deferred_query_errors"
        table = "deferred_error_author"


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_initialization_preserves_templates_and_replay_reports_their_errors():
    from hare.exceptions import QueryError
    from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

    declaration = DeferredErrorAuthor._declaration
    with HareContext():
        misspelled_filter = DeferredErrorAuthor.objects.filter(no_such_name="Ann")
        misspelled_ordering = DeferredErrorAuthor.objects.order_by("no_such_name")
        recursive_union = (
            DeferredErrorAuthor.objects.filter(name="Ann")
            .union(DeferredErrorAuthor.objects.filter(name="Bob"))
            .with_recursive("children")
        )
        valid = DeferredErrorAuthor.objects.filter(name="Ann")
    module = types.ModuleType("deferred_query_error_models")
    module.__models__ = [DeferredErrorAuthor]
    async with HareContext() as context:
        await context.init(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"deferred_query_errors": {"models": [module]}},
            },
            connect=False,
        )
        for query, error, message in (
            (misspelled_filter, FieldError, "no_such_name"),
            (misspelled_ordering, FieldError, "no_such_name"),
            (recursive_union, QueryError, "with_recursive"),
        ):
            with pytest.raises(error, match=message):
                CallsBeforeSetup.bind(query, context)
            assert query._binding is None and query._calls_before_setup is not None
        bound = CallsBeforeSetup.bind(valid, context)
        assert bound.sql() == context.apps.get_queryset(DeferredErrorAuthor).filter(name="Ann").sql()
        assert valid._binding is None and valid._calls_before_setup is not None
        assert DeferredErrorAuthor._declaration is declaration
