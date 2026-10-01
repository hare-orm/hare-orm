"""HareFastAPI: the Hare context of a FastAPI app, request queries as dependencies, ORM errors in
FastAPI's error format and a transaction per request."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from hare import Hare
from hare.contrib.frameworks import PageSchema, RequestTransaction
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import (
    FilterField,
    InPath,
    KeyColumns,
    OffsetPagination,
    OrderingConfig,
    RequestQuery,
    RequestQueryForbidden,
)
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError, DoesNotExist
from hare.query.enums import Lookup
from tests.contrib.frameworks.models import Chapter, Writer
from tests.contrib.frameworks.queries import NamedWriterQuery

CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
}


class WriterSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ChapterSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    volume: int
    number: int
    title: str


class WriterQuery(RequestQuery[Writer]):
    class Meta:
        queryset = Writer.objects.all()
        filters = (FilterField("name", lookups=(Lookup.ICONTAINS, Lookup.IN)),)
        ordering = OrderingConfig(fields=("name",), default=("name",))
        pagination = OffsetPagination(default_limit=2, max_limit=5)


class ChapterQuery(RequestQuery[Chapter]):
    pk: Annotated[KeyColumns[int, int], InPath()]

    class Meta:
        queryset = Chapter.objects.all()
        pagination = None


class ChaptersQuery(RequestQuery[Chapter]):
    class Meta:
        queryset = Chapter.objects.all()
        filters = (FilterField("pk", lookups=(Lookup.IN,)),)
        ordering = OrderingConfig(default=("title",))
        pagination = None


class TeapotError(Exception):
    """An error the application answers itself."""


async def answer_teapot(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"teapot": True}, status_code=418)


def build_app(**options: Any) -> HareFastAPI:
    lifespan_events: list[str] = options.pop("lifespan_events", [])

    @asynccontextmanager
    async def create_rows(app: FastAPI) -> AsyncGenerator[dict[str, Any]]:
        lifespan_events.append(f"context open: {HareContext.get_current() is not None}")
        await Hare.generate_schemas()
        anna = await Writer.objects.create(name="anna")
        boris = await Writer.objects.create(name="boris")
        await Writer.objects.create(name="carl")
        await Chapter.objects.create(volume=1, number=1, writer=anna, title="Opening")
        await Chapter.objects.create(volume=1, number=2, writer=boris, title="Middle")
        await Chapter.objects.create(volume=2, number=1, writer=anna, title="Ending")
        yield {"seeded": True}
        lifespan_events.append("stopped")

    app = HareFastAPI(hare_config=CONFIG, lifespan=create_rows, **options)
    app.add_exception_handler(TeapotError, answer_teapot)

    @app.get("/writers", response_model=PageSchema[WriterSchema])
    async def list_writers(writers: WriterQuery = RequestQueryDependency.provide(WriterQuery)) -> Any:
        return await writers.page()

    @app.get("/chapters/{pk}", response_model=ChapterSchema)
    async def one_chapter(chapter: ChapterQuery = RequestQueryDependency.provide(ChapterQuery)) -> Any:
        return await chapter.get()

    @app.get("/chapters", response_model=list[ChapterSchema])
    async def list_chapters(chapters: ChaptersQuery = RequestQueryDependency.provide(ChaptersQuery)) -> Any:
        return await chapters.fetch()

    @app.get("/named-writers")
    async def named_writers(
        writers: NamedWriterQuery = RequestQueryDependency.provide(NamedWriterQuery),
    ) -> list[str]:
        return [writer.name for writer in await writers.fetch()]

    @app.get("/seeded")
    async def seeded(request: Request) -> dict[str, Any]:
        return {"seeded": request.state.seeded}

    @app.post("/writers/{name}", status_code=201)
    async def create_writer(name: str) -> dict[str, str]:
        await Writer.objects.create(name=name)
        return {"name": name}

    @app.post("/writers/{name}/fail")
    async def create_and_fail(name: str) -> None:
        await Writer.objects.create(name=name)
        raise RuntimeError("the handler failed after writing")

    @app.post("/writers/{name}/missing")
    async def create_and_miss(name: str) -> None:
        await Writer.objects.create(name=name)
        await Writer.objects.get(name="nobody")

    @app.post("/writers/{name}/refused")
    async def create_and_refuse(name: str) -> None:
        await Writer.objects.create(name=name)
        raise HTTPException(status_code=403, detail="no")

    @app.post("/writers/{name}/answered")
    async def create_and_answer_by_own_handler(name: str) -> None:
        await Writer.objects.create(name=name)
        raise TeapotError

    @app.post("/writers/{name}/unavailable")
    async def create_and_answer_503(name: str) -> JSONResponse:
        await Writer.objects.create(name=name)
        return JSONResponse({"name": name}, status_code=503)

    @app.post("/writers/{name}/outside")
    @RequestTransaction.skip
    async def create_outside_the_transaction(name: str) -> JSONResponse:
        await Writer.objects.create(name=name)
        return JSONResponse({"name": name}, status_code=503)

    @app.get("/deleted-writers")
    async def deleted_writers() -> None:
        raise RequestQueryForbidden("deleted", "The request may not see deleted rows")

    return app


def names(client: TestClient) -> list[str]:
    writer_names: list[str] = []
    page_url: str | None = "/writers?limit=5"
    while page_url is not None:
        page = client.get(page_url).json()
        writer_names.extend(writer["name"] for writer in page["result"])
        page_url = page["next"]
    return writer_names


def test_the_context_is_open_around_the_apps_own_lifespan():
    lifespan_events: list[str] = []
    with TestClient(build_app(lifespan_events=lifespan_events)) as client:
        assert client.get("/seeded").json() == {"seeded": True}
    assert lifespan_events == ["context open: True", "stopped"]


def test_a_request_query_as_a_dependency():
    with TestClient(build_app()) as client:
        response = client.get("/writers", params=[("name__in", "anna"), ("name__in", "carl")])
        searched = client.get("/writers", params={"name__icontains": "OR"})
    assert response.json() == {
        "result": [{"id": 1, "name": "anna"}, {"id": 3, "name": "carl"}],
        "count": 2,
        "limit": 2,
        "offset": 0,
        "next": None,
        "previous": None,
    }
    assert [writer["name"] for writer in searched.json()["result"]] == ["boris"]


def test_composite_keys_from_the_path_and_from_repeated_parameters():
    with TestClient(build_app()) as client:
        one = client.get("/chapters/1,2")
        many = client.get("/chapters", params=[("pk__in", "1,1"), ("pk__in", "2,1")])
        missing = client.get("/chapters/9,9")
    assert one.json() == {"volume": 1, "number": 2, "title": "Middle"}
    assert [chapter["title"] for chapter in many.json()] == ["Ending", "Opening"]
    assert (missing.status_code, missing.json()) == (404, {"detail": "Not Found"})


def test_request_query_parameters_are_documented():
    with TestClient(build_app()) as client:
        schema = client.get("/openapi.json").json()
    parameters = {parameter["name"]: parameter for parameter in schema["paths"]["/writers"]["get"]["parameters"]}
    assert list(parameters) == ["name__icontains", "name__in", "ordering", "limit", "offset"]
    assert parameters["name__in"]["schema"]["anyOf"][0] == {"type": "array", "items": {"type": "string"}}
    assert parameters["name__in"]["description"] == "Repeat the parameter for each value."
    assert parameters["limit"]["schema"]["maximum"] == 5
    assert parameters["ordering"]["description"].startswith("Names to order by")
    path_parameters = schema["paths"]["/chapters/{pk}"]["get"]["parameters"]
    assert [(parameter["name"], parameter["in"], parameter["required"]) for parameter in path_parameters] == [
        ("pk", "path", True)
    ]


def test_invalid_parameters_answer_422_as_fastapi_does():
    with TestClient(build_app()) as client:
        refused = client.get("/writers", params={"ordering": "id"})
        own = client.get("/writers", params={"limit": 6})
        path = client.get("/chapters/1")
    assert refused.status_code == 422
    assert refused.json() == {
        "detail": [
            {
                "type": "ordering",
                "loc": ["query", "ordering"],
                "msg": "Can't order by 'id' - allowed: name",
                "input": "id",
            }
        ]
    }
    assert own.status_code == 422
    assert set(own.json()["detail"][0]) >= {"type", "loc", "msg", "input"}
    assert own.json()["detail"][0]["loc"] == ["query", "limit"]
    assert path.status_code == 422
    assert path.json()["detail"][0]["loc"][:2] == ["path", "pk"]


def test_orm_errors_and_a_forbidden_parameter():
    with TestClient(build_app()) as client:
        client.post("/writers/dora")
        duplicate = client.post("/writers/dora")
        forbidden = client.get("/deleted-writers")
    assert (duplicate.status_code, duplicate.json()) == (409, {"detail": "Conflict"})
    assert (forbidden.status_code, forbidden.json()) == (403, {"detail": "The request may not see deleted rows"})


def test_the_apps_own_exception_handler_wins():
    async def answer_missing(request: Request, exception: Exception) -> JSONResponse:
        return JSONResponse({"missing": True}, status_code=410)

    with TestClient(build_app(exception_handlers={DoesNotExist: answer_missing})) as client:
        response = client.get("/chapters/9,9")
    assert (response.status_code, response.json()) == (410, {"missing": True})


def test_atomic_requests_commit_or_roll_back():
    with TestClient(build_app(atomic_requests=True), raise_server_exceptions=False) as client:
        assert client.post("/writers/dora").status_code == 201
        assert client.post("/writers/emil/fail").status_code == 500
        assert client.post("/writers/fred/unavailable").status_code == 503
        assert client.post("/writers/gina/outside").status_code == 503
        assert client.post("/writers/hans/missing").status_code == 404
        assert client.post("/writers/ivan/refused").status_code == 403
        assert client.post("/writers/jack/answered").status_code == 418
        assert names(client) == ["anna", "boris", "carl", "dora", "gina"]


def test_without_atomic_requests_a_failed_request_keeps_its_writes():
    with TestClient(build_app(), raise_server_exceptions=False) as client:
        assert client.post("/writers/emil/fail").status_code == 500
        assert "emil" in names(client)


def test_atomic_requests_must_be_a_bool_or_connection_names():
    with pytest.raises(ConfigurationError, match="atomic_requests"):
        HareFastAPI(hare_config=CONFIG, atomic_requests="default")
    assert HareFastAPI(hare_config=CONFIG, atomic_requests=True).hare.transaction_connection_names == (None,)


def test_an_error_of_the_whole_query_is_answered_with_422():
    with TestClient(build_app()) as client:
        response = client.get("/named-writers?name=anna&name__in=anna&name__in=boris")
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "value_error",
            "loc": ["query"],
            "msg": "Value error, Give name or name__in, not both",
            "input": {"name": "anna", "name__in": ["anna", "boris"]},
        }
    ]


def test_a_request_query_built_for_a_model_is_a_dependency():
    Hare.bind_models(config=CONFIG)
    built_writer_query = RequestQuery.for_model(
        Writer,
        filters=(FilterField("name", lookups=(Lookup.IN,)),),
        ordering=OrderingConfig(default=("name",)),
    )
    app = build_app()

    @app.get("/built-writers", response_model=PageSchema[WriterSchema])
    async def built_writers(writers: Any = RequestQueryDependency.provide(built_writer_query)) -> Any:
        return await writers.page()

    with TestClient(app) as client:
        response = client.get("/built-writers", params={"name__in": ["carl", "anna"]})
    assert response.status_code == 200
    assert [row["name"] for row in response.json()["result"]] == ["anna", "carl"]
