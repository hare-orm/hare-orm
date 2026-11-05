from dataclasses import dataclass

import pytest
from litestar import Litestar, get
from litestar.dto import DataclassDTO, DTOConfig, _codegen_backend
from litestar.testing import TestClient

from hare import Model, fields
from hare.contrib.frameworks.litestar import HareDTO, HarePlugin
from hare.core.hare_context import HareContext
from hare.fields.relations.relation_accessors import RelationAccessors


class Publisher(Model):
    name = fields.TextField()


class Author(Model):
    name = fields.TextField()
    publisher = fields.ForeignKeyField("dto.Publisher", related_name="authors")


class Book(Model):
    title = fields.TextField()
    author = fields.ForeignKeyField("dto.Author", related_name="books")

    def title_length(self) -> int:
        return len(self.title)


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_local_backend_reads_nested_rows_without_replacing_litestar_codegen():
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"dto": {"models": [__name__]}}}
    context = HareContext()
    await context.init(config=config, connect=False, _enable_global_fallback=False)
    original_factory = _codegen_backend.TransferFunctionFactory
    assert original_factory.__module__ == "litestar.dto._codegen_backend"
    try:
        BookRow = context.apps.get_model("dto.Book")
        AuthorRow = context.apps.get_model("dto.Author")
        PublisherRow = context.apps.get_model("dto.Publisher")
        publisher = PublisherRow.construct(id=1, name="Press", _saved_in_db=True)
        author = AuthorRow.construct(id=2, name="Writer", _saved_in_db=True)
        author.publisher = publisher
        book = BookRow.construct(id=3, title="Books", _saved_in_db=True)
        book.author = author
        RelationAccessors.get_relation(author, "books").relation_rows.set_fetched([book])

        class BookDTO(HareDTO[BookRow]):
            config = DTOConfig(max_nested_depth=3)
            computed = ("title_length",)
            related_values = {"publisher_name": "author.publisher.name"}

        @get("/book", return_dto=BookDTO)
        async def read_book() -> BookRow:
            return book

        @get("/books", return_dto=BookDTO)
        async def read_books() -> list[BookRow]:
            return [book]

        @get("/by-title", return_dto=BookDTO)
        async def by_title() -> dict[str, BookRow]:
            return {"Books": book}

        @dataclass
        class Plain:
            title: str

        @get("/plain", return_dto=DataclassDTO[Plain])
        async def plain() -> Plain:
            return Plain("normal")

        app = Litestar(
            route_handlers=[read_book, read_books, by_title, plain],
            plugins=[HarePlugin(config, hare_context=context)],
            logging_config=None,
        )
        assert _codegen_backend.TransferFunctionFactory is original_factory
        with TestClient(app, raise_server_exceptions=True) as client:
            response = client.get("/book")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["title_length"] == 5 and payload["publisher_name"] == "Press", payload
            assert payload["author"]["publisher"]["name"] == "Press", payload
            assert payload["author"]["books"][0]["title"] == "Books", payload
            assert client.get("/books").json()[0]["publisher_name"] == "Press"
            assert client.get("/by-title").json()["Books"]["publisher_name"] == "Press"
            assert client.get("/plain").json() == {"title": "normal"}
        assert _codegen_backend.TransferFunctionFactory is original_factory
    finally:
        await context.close_connections()
