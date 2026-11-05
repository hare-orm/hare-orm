from typing import Annotated

import pytest
from pydantic import Field

from hare import Hare, Model, fields
from hare.contrib.request_query import NoFilter, RequestQuery


class ParameterBook(Model):
    id = fields.IntField(primary_key=True)


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "framework",
    [
        pytest.param("fastapi", marks=pytest.mark.requires("fastapi", "http")),
        pytest.param("litestar", marks=pytest.mark.requires("litestar")),
    ],
)
async def test_factories_run_per_request_after_validation_and_never_for_schema(framework):
    context = await Hare.init(
        {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"defaults": {"models": [__name__], "default_connection": "default"}},
        },
        connect=False,
    )
    calls = []

    def make_token(data):
        calls.append(data["seed"])
        return data["seed"] * 10 + len(calls)

    try:
        model = context.apps.get_model("defaults.ParameterBook")

        class BookQuery(RequestQuery[model]):
            seed: Annotated[int, NoFilter()] = 2
            token: Annotated[int, NoFilter()] = Field(default_factory=make_token, alias="request-token", gt=0)
            tags: Annotated[list[int], NoFilter()] = Field(default_factory=list)
            note: Annotated[str | None, NoFilter()] = Field(default_factory=lambda: "generated")

            class Meta:
                queryset = model.objects.all()

        description = next(item for item in BookQuery.describe_parameters() if item.name == "token")
        assert description.has_default_factory and not description.required and description.default is None
        assert BookQuery(seed=3, token=5, note=None).note is None
        assert calls == []
        if framework == "fastapi":
            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            from hare.contrib.frameworks.fastapi import HareExceptionHandlers, RequestQueryDependency

            app = FastAPI(exception_handlers=HareExceptionHandlers.get_handlers())

            @app.get("/books")
            async def fast_books(books: BookQuery = RequestQueryDependency.provide(BookQuery)):
                return {"token": books.token, "tags": books.tags, "note": books.note}

            schema_path = "/openapi.json"
            invalid_status = 422
        else:
            from litestar import Litestar, get
            from litestar.di import NamedDependency
            from litestar.testing import TestClient

            from hare.contrib.frameworks.litestar import HareExceptionHandlers, RequestQueryDIPlugin

            @get("/books", dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
            async def star_books(books: NamedDependency[BookQuery]) -> dict:
                return {"token": books.token, "tags": books.tags, "note": books.note}

            app = Litestar(
                [star_books], plugins=[RequestQueryDIPlugin()], exception_handlers=HareExceptionHandlers.get_handlers()
            )
            schema_path = "/schema/openapi.json"
            invalid_status = 400
        assert calls == []
        with TestClient(app) as client:
            for _ in range(2):
                schema = client.get(schema_path)
                assert schema.status_code == 200
                parameter = next(
                    item
                    for item in schema.json()["paths"]["/books"]["get"]["parameters"]
                    if item["name"] == "request-token"
                )
                assert not parameter["required"] and "default" not in parameter["schema"]
            assert calls == []
            for expected in (31, 32):
                response = client.get("/books?seed=3")
                assert response.status_code == 200
                assert response.json() == {"token": expected, "tags": [], "note": "generated"}
            response = client.get("/books?request-token=9&tags=1&tags=2")
            assert response.status_code == 200
            assert response.json()["token"] == 9 and response.json()["tags"] == [1, 2]
            assert client.get("/books?request-token=-1").status_code == invalid_status
            assert calls == [3, 3]
    finally:
        await Hare.close_connections()
        context.__exit__(None, None, None)
