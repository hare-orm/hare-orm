"""HareRobyn: the Hare context of a Robyn app, request queries its handlers take, responses
validated with the route's response model, ORM errors as HTTP answers and a transaction per
request."""

from typing import Annotated, Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import BaseModel, ConfigDict
from robyn import Request, Response
from robyn.robyn import QueryParams
from robyn.testing import TestClient

from hare import Hare
from hare.contrib.frameworks import PageSchema, RequestTransaction
from hare.contrib.frameworks.robyn import HareExceptionHandlers, HareRobyn, HareSubRouter
from hare.contrib.request_query import (
    FilterField,
    InPath,
    KeyColumns,
    OffsetPagination,
    OrderingConfig,
    Page,
    RequestQuery,
    RequestQueryForbidden,
)
from hare.exceptions import ConfigurationError
from hare.query.enums import Lookup
from tests.contrib.frameworks.models import Chapter, Writer
from tests.contrib.frameworks.queries import NamedWriterQuery

CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
}


class HareTestClient(TestClient):
    """Robyn's test client, starting and stopping the application as its server would, and
    taking a list of values for a repeated query parameter."""

    def __enter__(self) -> "HareTestClient":
        self._loop.run_until_complete(self.app.start_hare())
        return self

    def __exit__(self, *args: Any) -> None:
        self._loop.run_until_complete(self.app.stop_hare())
        super().__exit__(*args)

    def _build_request(self, method: str, path: str, query_params: dict[str, Any] | None = None, **kwargs: Any):
        request = super()._build_request(method, path, **kwargs)
        repeated = QueryParams()
        for name, value in (query_params or {}).items():
            for item in value if isinstance(value, list) else [value]:
                repeated.set(name, str(item))
        request.query_params = repeated
        return request


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


def build_app(**options: Any) -> HareRobyn:
    events: list[str] = options.pop("events", [])
    app = HareRobyn(__file__, hare_config=CONFIG, **options)

    @app.startup_handler
    async def create_rows() -> None:
        events.append("started")
        await Hare.generate_schemas()
        anna = await Writer.objects.create(name="anna")
        boris = await Writer.objects.create(name="boris")
        await Writer.objects.create(name="carl")
        await Chapter.objects.create(volume=1, number=1, writer=anna, title="Opening")
        await Chapter.objects.create(volume=1, number=2, writer=boris, title="Middle")
        await Chapter.objects.create(volume=2, number=1, writer=anna, title="Ending")

    @app.shutdown_handler
    def note_the_stop() -> None:
        events.append("stopped")

    @app.get("/writers", response_model=PageSchema[WriterSchema])
    async def list_writers(writers: WriterQuery) -> Page[Writer]:
        return await writers.page()

    @app.get("/named-writers")
    async def named_writers(writers: NamedWriterQuery) -> list[str]:
        return [writer.name for writer in await writers.fetch()]

    @app.post("/writers/:name", status_code=201)
    async def create_writer(request: Request) -> dict[str, str]:
        name = request.path_params["name"]
        await Writer.objects.create(name=name)
        return {"name": name}

    @app.post("/writers/:name/fail")
    async def create_and_fail(request: Request) -> None:
        await Writer.objects.create(name=request.path_params["name"])
        raise RuntimeError("the handler failed after writing")

    @app.post("/writers/:name/missing")
    async def create_and_miss(request: Request) -> None:
        await Writer.objects.create(name=request.path_params["name"])
        await Writer.objects.get(name="nobody")

    @app.post("/writers/:name/unavailable")
    async def create_and_answer_503(request: Request) -> Response:
        await Writer.objects.create(name=request.path_params["name"])
        return Response(503, {}, "unavailable")

    @app.post("/writers/:name/outside")
    @RequestTransaction.skip
    async def create_outside_the_transaction(request: Request) -> Response:
        await Writer.objects.create(name=request.path_params["name"])
        return Response(503, {}, "unavailable")

    @app.get("/deleted-writers")
    async def deleted_writers() -> None:
        raise RequestQueryForbidden("deleted", "The request may not see deleted rows")

    chapters = HareSubRouter(prefix="/chapters")

    @chapters.get("/:pk", response_model=ChapterSchema)
    async def one_chapter(chapter: ChapterQuery) -> Chapter:
        return await chapter.get()

    @chapters.get("/", response_model=list[ChapterSchema])
    async def list_chapters(chapters: ChaptersQuery) -> list[Chapter]:
        return await chapters.fetch()

    @chapters.post("/:volume/:number/rename")
    async def rename_chapter(request: Request) -> Response:
        chapter = await Chapter.objects.get(
            volume=int(request.path_params["volume"]), number=int(request.path_params["number"])
        )
        chapter.title = "Renamed"
        await chapter.save()
        return Response(503, {}, "unavailable")

    app.include_router(chapters)
    return app


def names(client: HareTestClient) -> list[str]:
    writer_names: list[str] = []
    while True:
        query_params = {"limit": "5", "offset": str(len(writer_names))}
        page_names = [writer["name"] for writer in client.get("/writers", query_params=query_params).json()["result"]]
        if not page_names:
            return writer_names
        writer_names.extend(page_names)


def test_the_context_is_open_around_the_apps_own_handlers():
    events: list[str] = []
    with HareTestClient(build_app(events=events)) as client:
        assert names(client) == ["anna", "boris", "carl"]
    assert events == ["started", "stopped"]


def test_a_request_query_a_handler_takes():
    with HareTestClient(build_app()) as client:
        page = client.get("/writers", query_params={"name__in": ["anna", "carl"], "limit": 1})
        searched = client.get("/writers", query_params={"name__icontains": "OR"})
    assert page.status_code == 200
    body = page.json()
    next_link = urlsplit(body.pop("next"))
    assert body == {"result": [{"id": 1, "name": "anna"}], "count": 2, "limit": 1, "offset": 0, "previous": None}
    assert (next_link.scheme, next_link.netloc, next_link.path) == ("http", "testclient", "/writers")
    assert sorted(parse_qsl(next_link.query)) == [
        ("limit", "1"),
        ("name__in", "anna"),
        ("name__in", "carl"),
        ("offset", "1"),
    ]
    assert [writer["name"] for writer in searched.json()["result"]] == ["boris"]


def test_composite_keys_from_the_path_and_from_repeated_parameters():
    with HareTestClient(build_app()) as client:
        one = client.get("/chapters/1,2")
        many = client.get("/chapters", query_params={"pk__in": ["1,1", "2,1"]})
        missing = client.get("/chapters/9,9")
    assert one.json() == {"volume": 1, "number": 2, "title": "Middle"}
    assert [chapter["title"] for chapter in many.json()] == ["Ending", "Opening"]
    assert (missing.status_code, missing.json()) == (404, {"detail": "Not Found"})


def test_request_query_parameters_are_documented():
    app = build_app()
    app._add_openapi_routes()
    paths = app.openapi.openapi_spec["paths"]
    parameters = {parameter["name"]: parameter for parameter in paths["/writers"]["get"]["parameters"]}
    assert list(parameters) == ["name__icontains", "name__in", "ordering", "limit", "offset"]
    assert parameters["name__in"]["in"] == "query"
    assert parameters["name__in"]["schema"]["anyOf"][0] == {"type": "array", "items": {"type": "string"}}
    assert parameters["name__in"]["description"] == "Repeat the parameter for each value."
    assert parameters["limit"]["schema"]["maximum"] == 5
    path_parameters = paths["/chapters/{pk}"]["get"]["parameters"]
    assert [(parameter["name"], parameter["in"], parameter["required"]) for parameter in path_parameters] == [
        ("pk", "path", True)
    ]


def test_invalid_parameters_answer_422_as_robyn_does():
    with HareTestClient(build_app()) as client:
        refused = client.get("/writers", query_params={"ordering": "id"})
        out_of_range = client.get("/writers", query_params={"limit": 6})
        path = client.get("/chapters/1")
    assert refused.status_code == 422
    assert refused.json() == {
        "error": "Validation Error",
        "detail": [
            {
                "type": "ordering",
                "loc": ["query", "ordering"],
                "msg": "Can't order by 'id' - allowed: name",
                "input": "id",
            }
        ],
    }
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"][0]["loc"] == ["query", "limit"]
    assert path.status_code == 422
    assert path.json()["detail"][0]["loc"][:2] == ["path", "pk"]


def test_orm_errors_and_a_forbidden_parameter():
    with HareTestClient(build_app()) as client:
        client.post("/writers/dora")
        duplicate = client.post("/writers/dora")
        forbidden = client.get("/deleted-writers")
    assert (duplicate.status_code, duplicate.json()) == (409, {"detail": "Conflict"})
    assert (forbidden.status_code, forbidden.json()) == (403, {"detail": "The request may not see deleted rows"})


def test_another_error_is_left_to_robyn():
    with pytest.raises(RuntimeError, match="failed after writing"):
        HareExceptionHandlers.handle(RuntimeError("the handler failed after writing"))


def test_atomic_requests_commit_or_roll_back():
    with HareTestClient(build_app(atomic_requests=True)) as client:
        assert client.post("/writers/dora").status_code == 201
        with pytest.raises(RuntimeError):
            client.post("/writers/emil/fail")
        assert client.post("/writers/fred/unavailable").status_code == 503
        assert client.post("/writers/gina/outside").status_code == 503
        assert client.post("/writers/hans/missing").status_code == 404
        assert client.post("/chapters/1/1/rename").status_code == 503
        assert names(client) == ["anna", "boris", "carl", "dora", "gina"]
        assert client.get("/chapters/1,1").json()["title"] == "Opening"


def test_without_atomic_requests_a_failed_request_keeps_its_writes():
    with HareTestClient(build_app()) as client:
        with pytest.raises(RuntimeError):
            client.post("/writers/emil/fail")
        assert "emil" in names(client)


def test_atomic_requests_must_be_a_bool_or_connection_names():
    with pytest.raises(ConfigurationError, match="atomic_requests"):
        HareRobyn(__file__, hare_config=CONFIG, atomic_requests="default")
    assert HareRobyn(__file__, hare_config=CONFIG, atomic_requests=True).hare.transaction_connection_names == (None,)


def test_an_error_of_the_whole_query_is_answered_with_422():
    with HareTestClient(build_app()) as client:
        response = client.get("/named-writers", query_params={"name": "anna", "name__in": ["anna", "boris"]})
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "value_error",
            "loc": ["query"],
            "msg": "Value error, Give name or name__in, not both",
            "input": {"name": "anna", "name__in": ["anna", "boris"]},
        }
    ]


def test_a_request_query_built_for_a_model_is_a_handlers_parameter():
    Hare.bind_models(config=CONFIG)
    built_writer_query = RequestQuery.for_model(
        Writer,
        filters=(FilterField("name", lookups=(Lookup.IN,)),),
        ordering=OrderingConfig(default=("name",)),
    )
    app = build_app()

    @app.get("/built-writers", response_model=PageSchema[WriterSchema])
    async def built_writers(writers: built_writer_query) -> Page[Writer]:  # type: ignore[valid-type]
        return await writers.page()

    with HareTestClient(app) as client:
        response = client.get("/built-writers", query_params={"name__in": ["carl", "anna"]})
    assert response.status_code == 200
    assert [row["name"] for row in response.json()["result"]] == ["anna", "carl"]
