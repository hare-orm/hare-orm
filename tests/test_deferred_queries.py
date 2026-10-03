"""Queries built before Hare.init() has set their model up record their calls; the calls are
replayed when init finishes, so a queryset declared at module level keeps working, and every
invalid one is reported by init with the place it was built at."""

import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError, FieldError
from hare.models import Model
from hare.query.expressions import OuterRef, Q, Subquery
from hare.query.functions import Count
from hare.query.queryset import QuerySet
from hare.query.relation_loading.prefetch import Prefetch

MODULE_NAME = "tests._deferred_query_models"


class DeferredAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=40)

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
                DeferredBook.objects.filter(author_id=OuterRef("id")).order_by("title").values("title")[:1]
            )
        )
        .order_by("id")
    ),
    "book count": lambda: DeferredBook.objects.filter(author__name="Ann").count(),
    "union": lambda: DeferredBook.objects.filter(title="B1").union(DeferredBook.objects.filter(author__name="Bob")),
}
MODULE_LEVEL_QUERIES = {name: build() for name, build in QUERIES_BUILT_BEFORE_INIT.items()}
MODULE_LEVEL_MODEL_SHORTCUTS = {
    "get": DeferredBook.objects.get(title="B2"),
    "get with Q": DeferredBook.objects.get(Q(title="B3")),
    "get_or_none": DeferredBook.objects.get_or_none(title="missing"),
    "exists": DeferredBook.objects.filter(author__name="Bob").exists(),
}


def get_context_config():
    module = types.ModuleType(MODULE_NAME)
    module.DeferredAuthor = DeferredAuthor  # type: ignore[attr-defined]
    module.DeferredBook = DeferredBook  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    return {
        "connections": {"default": "sqlite://:memory:"},
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
        bob = await DeferredAuthor.objects.create(id=2, name="Bob")
        await DeferredBook.objects.create(id=1, title="B1", author=ann)
        await DeferredBook.objects.create(id=2, title="B2", author=ann)
        await DeferredBook.objects.create(id=3, title="B3", author=bob)
        yield context
    finally:
        await context.connections.close_all(discard=True)
        await context.__aexit__(None, None, None)
        sys.modules.pop(MODULE_NAME, None)


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


@pytest.mark.asyncio
async def test_init_reports_every_invalid_query_built_before_it():
    misspelled_filter = DeferredErrorAuthor.objects.filter(no_such_name="Ann")
    misspelled_ordering = DeferredErrorAuthor.objects.all().order_by("no_such_name")
    valid = DeferredErrorAuthor.objects.filter(name="Ann")
    with pytest.raises(ConfigurationError, match="Hare ORM is not initialized"):
        valid.sql()
    module_name = "tests._deferred_query_error_models"
    module = types.ModuleType(module_name)
    module.DeferredErrorAuthor = DeferredErrorAuthor  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    context = HareContext()
    await context.__aenter__()
    try:
        with pytest.raises(ConfigurationError) as error:
            await context.init(
                config={
                    "connections": {"default": "sqlite://:memory:"},
                    "apps": {"deferred_query_errors": {"models": [module_name], "default_connection": "default"}},
                }
            )
        message = str(error.value)
        assert message.startswith("Queries built before Hare.init() are invalid:")
        assert f"{__file__}:" in message
        assert (
            "DeferredErrorAuthor.objects.filter(no_such_name=...): DeferredErrorAuthor has no field 'no_such_name'"
            in message
        )
        assert "Unknown field no_such_name for ordering: DeferredErrorAuthor has no field 'no_such_name'" in message
        with pytest.raises(FieldError, match="has no field 'no_such_name'"):
            misspelled_filter.sql()
        with pytest.raises(FieldError, match="Unknown field no_such_name"):
            misspelled_ordering.sql()
    finally:
        await context.__aexit__(None, None, None)
        sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_first_level_names_are_checked_when_called_after_init(deferred_context):
    with pytest.raises(
        FieldError,
        match=r"DeferredBook.objects.filter\(no_such_title=...\): DeferredBook has no field 'no_such_title'",
    ):
        DeferredBook.objects.filter(no_such_title="B1")
    with pytest.raises(
        FieldError, match=r"DeferredBook.objects.exclude\(title__icontainz=...\): DeferredBook.title has"
    ):
        DeferredBook.objects.exclude(title__icontainz="B")
    with pytest.raises(
        FieldError, match=r"DeferredBook.objects.values\('no_such_title'\): DeferredBook has no field 'no_such_title'"
    ):
        DeferredBook.objects.all().values("no_such_title")
    with pytest.raises(FieldError, match=r"DeferredBook.objects.values_list\('no_such_title'\)"):
        DeferredBook.objects.all().values_list("no_such_title")
    with pytest.raises(FieldError, match=r"DeferredBook.objects.only\('no_such_title'\)"):
        DeferredBook.objects.all().only("no_such_title")


@pytest.mark.asyncio
async def test_deeper_names_are_reported_with_their_full_path(deferred_context):
    with pytest.raises(
        FieldError,
        match=r"DeferredBook.objects.filter\(author__no_such_name=...\): DeferredAuthor has no field 'no_such_name'",
    ):
        DeferredBook.objects.filter(author__no_such_name="Ann")
    with pytest.raises(
        FieldError, match="Unknown field author__no_such_name for ordering: DeferredAuthor has no field"
    ):
        await DeferredBook.objects.all().order_by("author__no_such_name")
    with pytest.raises(
        FieldError, match='Unknown field "author__no_such_name": DeferredAuthor has no field "no_such_name"'
    ):
        await DeferredBook.objects.all().values("author__no_such_name")
