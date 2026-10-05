# FastAPI

`hare.contrib.frameworks.fastapi` connects hare to a [FastAPI](https://fastapi.tiangolo.com) application.
Install it with the `fastapi` extra (FastAPI 0.135 or newer, pydantic).

```python
from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://library:secret@localhost:5432/library"},
    "apps": {"library": {"models": ["library.models"], "default_connection": "default"}},
}

app = HareFastAPI(hare_config=HARE_CONFIG, atomic_requests=True, title="Books")


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: BookQuery = RequestQueryDependency.provide(BookQuery)) -> Page[Book]:
    return await books.page()
```

## <a id="harefastapi"></a>`HareFastAPI`

```python
class HareFastAPI(FastAPI):
    def __init__(
        self,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        atomic_requests: bool | Sequence[str] = False,
        lifespan: Lifespan | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None: ...
```

`HareFastAPI` is a `FastAPI` application; `hare_config` is what `Hare.init(config=...)` takes, `kwargs`
what `FastAPI()` takes.

- **The models are bound when the application is created** (`Hare.bind_models()`, no connection). FastAPI
  reads a route's parameters when the route is declared, and a request query's parameters —
  [`Meta.filters`](request-queries.md#meta-filters), the descriptions of its filters — are read from the
  models. A router declared in a module imported before the application is created needs
  `Hare.bind_models(HARE_CONFIG)` called before that import.
- **The `HareContext`.** Opened when the application starts, before its own `lifespan` — which already
  has the database — and closed when it stops. The context is the global fallback, so every request sees
  it. On shutdown `Hare.close_connections()` also waits for the background observers still running.
- **Request queries are checked at startup.** `RequestQuery.check_declarations()` runs once the models
  are set up: a wrong [request query](request-queries.md) stops the application from starting.
- **ORM errors as HTTP answers**, in FastAPI's own error format, unless the application answers the error
  itself (`exception_handlers=`):

  | Error | Answer |
  |---|---|
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (unique, foreign key, protected relation) | `409 Conflict`, `{"detail": "Conflict"}` — without the database's message, which names tables and constraints |
  | `ValidationError` (a value a field refuses: out of its range, too long, a null byte) | `422 Unprocessable Content`, the field's message in `detail` |
  | `InvalidRequestQuery` | `422 Unprocessable Content` as FastAPI answers invalid parameters itself: `{"detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; an error of the whole query (a model validator's) has `"loc": ["query"]` and the query's parameters as `input` |
  | `RequestQueryForbidden` (a parameter asks for what the request may not see: deleted rows without `may_see_deleted()`) | `403 Forbidden`, the reason in `detail` |

  An application mounted on it (`app.mount("/v1", sub_app)`) answers its own errors: give it the same
  answers with `FastAPI(exception_handlers=HareExceptionHandlers.get_handlers())`.

- **A request's own state.** Each request starts with an empty count of `RepeatedQueryDetector`. Only its
  own writes send its reads to the connection written through ([reading your own
  writes](../connections/multiple-databases.md#read-your-writes)) — not the writes of the requests its
  task served before or of the application's startup.
- **A transaction per request** with `atomic_requests` — see below.

## <a id="request-queries-as-dependencies"></a>Request queries as dependencies

`RequestQueryDependency.provide(QueryClass)` is the `Depends()` building a request query from the
request. Each parameter of the class is a parameter of the route, validated by FastAPI and listed in the
OpenAPI schema with its type, default, limits, enum and description:

- a query parameter — a list one repeated, `?status__in=draft&status__in=published`;
- a path parameter for one marked `InPath()`:

```python
class ChapterQuery(RequestQuery[Chapter]):
    pk: Annotated[KeyColumns[int, int], InPath()]

    class Meta:
        queryset = Chapter.objects.all()
        pagination = None


@app.get("/chapters/{pk}", response_model=ChapterSchema)
async def get_chapter(chapter: ChapterQuery = RequestQueryDependency.provide(ChapterQuery)) -> Chapter:
    return await chapter.get()
```

`KeyColumns` and `CommaSeparated` reach the query as the text of the parameter, which the query parses.
The query gets the request: `query.request`, and the addresses of a page's `next` and `previous`. A value
the query refuses — an ordering it doesn't allow, a malformed cursor — is answered as FastAPI answers an
invalid parameter.

## <a id="responses"></a>Responses

FastAPI validates what a route returns with its `response_model` from its attributes and documents it —
the schemas of a row, a list and a page — see [Web frameworks](web-frameworks.md#response-schemas).

## <a id="atomic-requests"></a>A transaction per request: `atomic_requests`

`HareFastAPI(..., atomic_requests=True)` runs each HTTP request in a transaction; a route whose endpoint
is marked `RequestTransaction.skip` runs without it:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(request: Request) -> None: ...
```

When the transaction commits or rolls back, and what else runs without it — see
[Web frameworks](web-frameworks.md#atomic-requests).

## <a id="health-routes"></a>Health routes: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = HareFastAPI(hare_config=HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))
```

The readiness and liveness routes it adds — see [Web frameworks](web-frameworks.md#health-routes).
