"""HarePlugin: the Hare context of a Litestar app, request queries as dependencies, ORM errors as
HTTP answers and a transaction per request."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import pytest
from litestar import Litestar, Request, Response, get, post
from litestar.di import NamedDependency
from litestar.exceptions import PermissionDeniedException
from litestar.params import FromPath
from litestar.testing import AsyncTestClient

from hare import Hare
from hare.contrib.frameworks import RequestTransaction
from hare.contrib.frameworks.litestar import (
    SKIP_TRANSACTION_OPT_KEY,
    HarePlugin,
    RequestQueryDIPlugin,
)
from hare.contrib.repeated_queries import RepeatedQueryDetector
from hare.contrib.request_query import (
    CommaSeparated,
    Filter,
    FilterField,
    KeyColumns,
    OffsetPagination,
    OrderingConfig,
    Page,
    RequestQuery,
    RequestQueryForbidden,
)
from hare.exceptions import ConfigurationError, DoesNotExist
from hare.query.enums import Lookup
from tests.contrib.frameworks.litestar.broken_models import Draft
from tests.contrib.frameworks.models import Chapter, Writer
from tests.contrib.frameworks.queries import NamedWriterQuery

CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
}


class WriterQuery(RequestQuery[Writer]):
    name__icontains: str | None = None
    ids: Annotated[CommaSeparated[int] | None, Filter("id", lookup="in")] = None

    class Meta:
        queryset = Writer.objects.all()
        ordering = OrderingConfig(fields=("name",), default=("name",))
        pagination = OffsetPagination(default_limit=2, max_limit=5)


class ChapterQuery(RequestQuery[Chapter]):
    pk: FromPath[KeyColumns[int, int]]

    class Meta:
        queryset = Chapter.objects.all().select_related("writer")
        pagination = None


class ChaptersQuery(RequestQuery[Chapter]):
    pk__in: list[KeyColumns[int, int]] | None = None

    class Meta:
        queryset = Chapter.objects.all()
        pagination = None


@get("/writers", dependencies={"writers": RequestQueryDIPlugin.provide(WriterQuery)})
async def list_writers(writers: NamedDependency[WriterQuery]) -> dict[str, Any]:
    page = await writers.page()
    return {"names": [writer.name for writer in page.result], "count": page.count, "next": page.next}


@get("/chapters/{pk:str}", dependencies={"chapter": RequestQueryDIPlugin.provide(ChapterQuery)})
async def one_chapter(chapter: NamedDependency[ChapterQuery]) -> dict[str, Any]:
    item = await chapter.get()
    return {"title": item.title, "writer": item.writer.name}


@get("/chapters", dependencies={"chapters": RequestQueryDIPlugin.provide(ChaptersQuery)})
async def list_chapters(chapters: NamedDependency[ChaptersQuery]) -> list[str]:
    return sorted(item.title for item in await chapters.fetch())


@get("/named-writers", dependencies={"writers": RequestQueryDIPlugin.provide(NamedWriterQuery)})
async def named_writers(writers: NamedDependency[NamedWriterQuery]) -> list[str]:
    return [writer.name for writer in await writers.fetch()]


@post("/writers/{name:str}")
async def create_writer(name: FromPath[str]) -> dict[str, str]:
    await Writer.objects.create(name=name)
    return {"name": name}


@post("/writers/{name:str}/fail")
async def create_and_fail(name: FromPath[str]) -> None:
    await Writer.objects.create(name=name)
    raise RuntimeError("the handler failed after writing")


@post("/writers/{name:str}/missing")
async def create_and_miss(name: FromPath[str]) -> None:
    await Writer.objects.create(name=name)
    await Writer.objects.get(name="nobody")


@post("/writers/{name:str}/refused")
async def create_and_refuse(name: FromPath[str]) -> None:
    await Writer.objects.create(name=name)
    raise PermissionDeniedException("no")


@post("/writers/{name:str}/unavailable")
async def create_and_answer_503(name: FromPath[str]) -> Response[dict[str, str]]:
    await Writer.objects.create(name=name)
    return Response({"name": name}, status_code=503)


@post("/writers/{name:str}/outside", opt={SKIP_TRANSACTION_OPT_KEY: True})
async def create_outside_the_transaction(name: FromPath[str]) -> Response[dict[str, str]]:
    await Writer.objects.create(name=name)
    return Response({"name": name}, status_code=503)


@get("/shape-counts")
async def shape_counts(request: Request[Any, Any, Any]) -> dict[str, Any]:
    return {"counts": RepeatedQueryDetector.current_counts.get()}


ROUTES = [
    list_writers,
    named_writers,
    one_chapter,
    list_chapters,
    create_writer,
    create_and_fail,
    create_and_miss,
    create_and_refuse,
    create_and_answer_503,
    create_outside_the_transaction,
    shape_counts,
]


@asynccontextmanager
async def create_schema(app: Litestar) -> AsyncGenerator[None]:
    await Hare.generate_schemas()
    yield


def build_app(**plugin_options: Any) -> Litestar:
    return Litestar(ROUTES, plugins=[HarePlugin(CONFIG, **plugin_options)], lifespan=[create_schema])


async def names(client: AsyncTestClient) -> list[str]:
    response = await client.get("/writers", params={"limit": 5})
    return response.json()["names"]


@pytest.mark.asyncio
async def test_the_context_is_open_for_the_apps_lifespan_and_requests():
    async with AsyncTestClient(build_app()) as client:
        assert (await client.post("/writers/anna")).status_code == 201
        assert await names(client) == ["anna"]
    assert not Hare.is_inited()


@pytest.mark.asyncio
async def test_a_request_query_as_a_dependency():
    async with AsyncTestClient(build_app()) as client:
        for name in ("carl", "anna", "boris"):
            await client.post(f"/writers/{name}")
        response = await client.get("/writers", params={"ordering": "-name"})
        assert response.json() == {
            "names": ["carl", "boris"],
            "count": 3,
            "next": "http://testserver.local/writers?ordering=-name&offset=2",
        }
        response = await client.get("/writers", params={"ids": "1,3", "name__icontains": "b"})
        assert response.json()["names"] == ["boris"]


@asynccontextmanager
async def create_chapters(app: Litestar) -> AsyncGenerator[None]:
    anna = await Writer.objects.create(name="anna")
    boris = await Writer.objects.create(name="boris")
    await Chapter.objects.create(volume=1, number=1, writer=anna, title="Opening")
    await Chapter.objects.create(volume=1, number=2, writer=boris, title="Middle")
    await Chapter.objects.create(volume=2, number=1, writer=anna, title="Ending")
    yield


@pytest.mark.asyncio
async def test_composite_keys_from_the_path_and_from_repeated_parameters():
    app = Litestar(ROUTES, plugins=[HarePlugin(CONFIG)], lifespan=[create_schema, create_chapters])
    async with AsyncTestClient(app) as client:
        assert (await client.get("/chapters/1,2")).json() == {"title": "Middle", "writer": "boris"}
        response = await client.get("/chapters", params={"pk__in": ["1,1", "2,1"]})
        assert response.json() == ["Ending", "Opening"]
        assert (await client.get("/chapters/9,9")).status_code == 404


@pytest.mark.asyncio
async def test_request_query_parameters_are_documented():
    async with AsyncTestClient(build_app()) as client:
        schema = (await client.get("/schema/openapi.json")).json()
    parameters = {parameter["name"]: parameter for parameter in schema["paths"]["/writers"]["get"]["parameters"]}
    assert set(parameters) == {"name__icontains", "ids", "ordering", "limit", "offset"}
    assert parameters["limit"]["schema"]["maximum"] == 5
    assert not parameters["offset"].get("required", False)
    assert schema["paths"]["/chapters/{pk}"]["get"]["parameters"][0]["in"] == "path"


class FilteredChapterQuery(RequestQuery[Chapter]):
    class Meta:
        queryset = Chapter.objects.all()
        filters = (
            FilterField("writer", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("title", lookups=(Lookup.ICONTAINS,)),
            FilterField("pk", lookups=(Lookup.IN,)),
        )
        ordering = OrderingConfig(default=("number",))
        pagination = None


@pytest.mark.asyncio
async def test_meta_filters_are_parameters_before_the_application_starts():
    @get("/filtered-chapters", dependencies={"chapters": RequestQueryDIPlugin.provide(FilteredChapterQuery)})
    async def filtered_chapters(chapters: NamedDependency[FilteredChapterQuery]) -> list[str]:
        return [chapter.title for chapter in await chapters.fetch()]

    app = Litestar(
        [*ROUTES, filtered_chapters], plugins=[HarePlugin(CONFIG)], lifespan=[create_schema, create_chapters]
    )
    assert {"writer", "writer__in", "title__icontains", "pk__in"} <= set(FilteredChapterQuery.model_fields)
    async with AsyncTestClient(app) as client:
        schema = (await client.get("/schema/openapi.json")).json()
        by_writer = await client.get("/filtered-chapters", params=[("writer__in", "1"), ("title__icontains", "ing")])
        by_key = await client.get("/filtered-chapters", params=[("pk__in", "1,2"), ("pk__in", "2,1")])
    parameters = {
        parameter["name"]: parameter for parameter in schema["paths"]["/filtered-chapters"]["get"]["parameters"]
    }
    assert set(parameters) == {"writer", "writer__in", "title__icontains", "pk__in"}
    assert parameters["writer"]["schema"]["oneOf"][0]["type"] == "integer"
    assert parameters["writer__in"]["description"] == "Repeat the parameter for each value."
    assert by_writer.json() == ["Opening", "Ending"]
    assert by_key.json() == ["Ending", "Middle"]


@pytest.mark.asyncio
async def test_invalid_parameters_answer_400_with_each_error():
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/writers", params={"ordering": "id"})
        own_response = await client.get("/writers", params={"limit": 6})
        path_response = await client.get("/chapters/1")
    assert response.status_code == 400
    assert response.json() == {
        "status_code": 400,
        "detail": "Validation failed for GET /writers",
        "extra": [{"message": "Can't order by 'id' - allowed: name", "key": "ordering", "source": "query"}],
    }
    # Litestar's own validation answers in the same shape.
    assert own_response.status_code == 400
    assert set(own_response.json()) == {"status_code", "detail", "extra"}
    assert set(own_response.json()["extra"][0]) == {"message", "key", "source"}
    assert path_response.status_code == 400
    assert path_response.json()["extra"][0]["key"] == "pk.1"
    assert path_response.json()["extra"][0]["source"] == "path"


@pytest.mark.asyncio
async def test_a_forbidden_parameter_answers_403():
    @get("/deleted-writers")
    async def deleted_writers() -> None:
        raise RequestQueryForbidden("deleted", "The request may not see deleted rows")

    app = Litestar([deleted_writers], plugins=[HarePlugin(CONFIG)])
    async with AsyncTestClient(app) as client:
        response = await client.get("/deleted-writers")
    assert response.status_code == 403
    assert response.json() == {
        "status_code": 403,
        "detail": "The request may not see deleted rows",
        "extra": {"parameter": "deleted"},
    }


@pytest.mark.asyncio
async def test_orm_errors_answer_404_and_409():
    async with AsyncTestClient(build_app()) as client:
        await client.post("/writers/anna")
        duplicate = await client.post("/writers/anna")
        assert duplicate.status_code == 409
        assert "writer" not in duplicate.text.lower()
        assert (await client.get("/chapters/5,5")).status_code == 404


@pytest.mark.asyncio
async def test_the_apps_own_exception_handler_wins():
    def answer_missing(request: Request[Any, Any, Any], exception: DoesNotExist) -> Response[dict[str, str]]:
        return Response({"missing": "yes"}, status_code=410)

    app = Litestar(
        ROUTES,
        plugins=[HarePlugin(CONFIG)],
        lifespan=[create_schema],
        exception_handlers={DoesNotExist: answer_missing},
    )
    async with AsyncTestClient(app) as client:
        response = await client.get("/chapters/5,5")
    assert (response.status_code, response.json()) == (410, {"missing": "yes"})


@pytest.mark.asyncio
async def test_atomic_requests_commit_or_roll_back():
    async with AsyncTestClient(build_app(atomic_requests=True)) as client:
        assert (await client.post("/writers/anna")).status_code == 201
        assert (await client.post("/writers/boris/fail")).status_code == 500
        assert (await client.post("/writers/clara/unavailable")).status_code == 503
        assert (await client.post("/writers/dora/missing")).status_code == 404
        assert (await client.post("/writers/emil/refused")).status_code == 403
        assert (await client.post("/writers/anna")).status_code == 409
        assert await names(client) == ["anna"]


@pytest.mark.asyncio
async def test_a_route_can_run_outside_the_transaction():
    async with AsyncTestClient(build_app(atomic_requests=True)) as client:
        assert (await client.post("/writers/dora/outside")).status_code == 503
        assert await names(client) == ["dora"]


@pytest.mark.asyncio
async def test_without_atomic_requests_a_failed_request_keeps_its_writes():
    async with AsyncTestClient(build_app()) as client:
        assert (await client.post("/writers/boris/fail")).status_code == 500
        assert await names(client) == ["boris"]


@pytest.mark.asyncio
async def test_atomic_requests_on_named_connections():
    config = {
        "connections": {"default": "sqlite://:memory:", "reports": "sqlite://:memory:"},
        "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
    }
    app = Litestar(
        ROUTES, plugins=[HarePlugin(config, atomic_requests=["default", "reports"])], lifespan=[create_schema]
    )
    async with AsyncTestClient(app) as client:
        assert (await client.post("/writers/boris/fail")).status_code == 500
        assert (await client.post("/writers/anna")).status_code == 201
        assert await names(client) == ["anna"]


@pytest.mark.asyncio
async def test_atomic_requests_naming_an_unknown_connection_fails_at_startup():
    plugin = HarePlugin(CONFIG, atomic_requests=["reports"])
    with pytest.raises(ConfigurationError, match="names the connection 'reports'"):
        async with plugin.lifespan(Litestar([], plugins=[plugin])):
            pass


def test_atomic_requests_must_be_a_bool_or_connection_names():
    with pytest.raises(ConfigurationError):
        HarePlugin(CONFIG, atomic_requests="default")
    with pytest.raises(ConfigurationError):
        HarePlugin(CONFIG, atomic_requests=["default", "default"])
    assert HarePlugin(CONFIG, atomic_requests=True).lifecycle.transaction_connection_names == (None,)
    assert HarePlugin(CONFIG).lifecycle.transaction_connection_names == ()


@pytest.mark.asyncio
async def test_a_wrong_request_query_fails_at_startup():
    class DraftQuery(RequestQuery[Draft]):
        headline: str | None = None

        class Meta:
            queryset = Draft.objects.all()

    config = {
        "connections": {"default": "sqlite://:memory:"},
        "apps": {
            "drafts": {"models": ["tests.contrib.frameworks.litestar.broken_models"], "default_connection": "default"}
        },
    }
    plugin = HarePlugin(config)
    with pytest.raises(ConfigurationError, match="DraftQuery.headline"):
        async with plugin.lifespan(Litestar([], plugins=[plugin])):
            pass


@pytest.mark.asyncio
async def test_each_request_counts_its_own_repeated_queries():
    async with AsyncTestClient(build_app()) as client:
        assert (await client.get("/shape-counts")).json() == {"counts": {}}


def test_the_request_query_plugin_comes_before_litestars_pydantic_one():
    app = build_app()
    assert isinstance(app.plugins.di[0], RequestQueryDIPlugin)
    assert sum(isinstance(plugin, RequestQueryDIPlugin) for plugin in app.plugins.di) == 1


@pytest.mark.asyncio
async def test_a_failed_commit_is_answered_with_an_error(monkeypatch):
    async def fail_to_commit(transaction: RequestTransaction, status_code: int) -> None:
        await transaction.close(RuntimeError("rolled back instead"))
        raise RuntimeError("the commit failed")

    # The error surfaces from the application's own handler - the test client shouldn't re-raise it.
    async with AsyncTestClient(build_app(atomic_requests=True), raise_server_exceptions=False) as client:
        monkeypatch.setattr(RequestTransaction, "finish", fail_to_commit)
        response = await client.post("/writers/anna")
        assert response.status_code == 500
        assert "anna" not in response.text
        monkeypatch.undo()
        assert await names(client) == []


@pytest.mark.asyncio
async def test_a_handler_returning_a_page_of_rows():
    @get("/writer-pages", dependencies={"writers": RequestQueryDIPlugin.provide(WriterQuery)})
    async def writer_pages(writers: NamedDependency[WriterQuery]) -> Page[Writer]:
        return await writers.page()

    app = Litestar([*ROUTES, writer_pages], plugins=[HarePlugin(CONFIG)], lifespan=[create_schema])
    async with AsyncTestClient(app) as client:
        for name in ("anna", "boris", "carl"):
            await client.post(f"/writers/{name}")
        response = await client.get("/writer-pages")
        schema = (await client.get("/schema/openapi.json")).json()
    assert response.json() == {
        "result": [
            {"id": 1, "name": "anna", "tags": None, "chapters": None},
            {"id": 2, "name": "boris", "tags": None, "chapters": None},
        ],
        "count": 3,
        "limit": 2,
        "offset": 0,
        "next": "http://testserver.local/writer-pages?offset=2",
        "previous": None,
    }
    reference = schema["paths"]["/writer-pages"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    component = schema["components"]["schemas"][reference["$ref"].rsplit("/", 1)[-1]]
    assert set(component["properties"]) == {"result", "count", "limit", "offset", "next", "previous"}


@pytest.mark.asyncio
async def test_an_error_of_the_whole_query_is_answered_with_400():
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/named-writers?name=anna&name__in=anna&name__in=boris")
    assert response.status_code == 400
    assert response.json()["extra"] == [{"message": "Value error, Give name or name__in, not both", "source": "query"}]


@pytest.mark.asyncio
async def test_a_request_query_built_for_a_model_is_a_dependency():
    Hare.bind_models(config=CONFIG)
    built_writer_query = RequestQuery.for_model(
        Writer,
        filters=(FilterField("name", lookups=(Lookup.IN,)),),
        ordering=OrderingConfig(default=("name",)),
    )

    @get("/built-writers", dependencies={"writers": RequestQueryDIPlugin.provide(built_writer_query)})
    async def built_writers(writers: NamedDependency[built_writer_query]) -> Page[Writer]:  # type: ignore[valid-type]
        return await writers.page()

    app = Litestar([*ROUTES, built_writers], plugins=[HarePlugin(CONFIG)], lifespan=[create_schema])
    async with AsyncTestClient(app) as client:
        for name in ("carl", "anna", "boris"):
            await client.post(f"/writers/{name}")
        response = await client.get("/built-writers", params={"name__in": ["carl", "anna"]})
    assert response.status_code == 200
    assert [row["name"] for row in response.json()["result"]] == ["anna", "carl"]
