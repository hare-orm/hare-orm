# Robyn

`hare.contrib.frameworks.robyn` connects hare to a [Robyn](https://robyn.tech) application. Install it
with the `robyn` extra (Robyn 0.88, pydantic).

```python
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.robyn import HareRobyn
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://library:secret@localhost:5432/library"},
    "apps": {"library": {"models": ["library.models"], "default_connection": "default"}},
}

app = HareRobyn(__file__, hare_config=HARE_CONFIG, atomic_requests=True)


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: BookQuery) -> Page[Book]:
    return await books.page()
```

## <a id="harerobyn"></a>`HareRobyn`

```python
class HareRobyn(Robyn):
    def __init__(
        self,
        file_object: str,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        atomic_requests: bool | Sequence[str] = False,
        openapi: OpenAPI | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None: ...
```

`HareRobyn` is a `Robyn` application; `hare_config` is what `Hare.init(config=...)` takes, `file_object`
and `kwargs` what `Robyn()` takes.

- **The `HareContext`.** The models are bound when the application is created; the context opens when a
  worker process starts — before the application's own `startup_handler` — and closes when it stops,
  after its own `shutdown_handler`. The context is the global fallback, so every request sees it. On
  shutdown `Hare.close_connections()` also waits for the background observers still running.
- **Request queries are checked at startup.** `RequestQuery.check_declarations()` runs once the models
  are set up: a wrong [request query](request-queries.md) stops the process from starting.
- **Request queries for handlers** and their parameters in the OpenAPI schema — see below.
- **Responses validated with the route's `response_model`** — see below.
- **ORM errors as HTTP answers** (`HareExceptionHandlers`):

  | Error | Answer |
  |---|---|
  | `InvalidRequestQuery` | `422 Unprocessable Content` as Robyn answers an invalid pydantic body itself: `{"error": "Validation Error", "detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; an error of the whole query (a model validator's) has `"loc": ["query"]` and the query's parameters as `input` |
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (unique, foreign key, protected relation) | `409 Conflict`, `{"detail": "Conflict"}` — without the database's message, which names tables and constraints |
  | `TransactionRetryError` (a serialization failure or deadlock — the request may be sent again) | `409 Conflict`, `{"detail": "Conflict"}` |
  | `ValidationError` (a value a field refuses: out of its range, too long, a null byte PostgreSQL refuses) | `422 Unprocessable Content`, the field's message in `detail` |
  | `RequestQueryForbidden` (a parameter asks for what the request may not see: deleted rows without `may_see_deleted()`) | `403 Forbidden`, the reason in `detail` |
  | `ApiGetterError` (a value of another service a request query's `Meta.api_getter` requires couldn't be loaded) | `502 Bad Gateway`, without the error's message, which names the service and the values asked |

  Any other error is left to Robyn (`500`). Robyn keeps one exception handler per application and hands
  it to each route when the route is declared: an application answering errors itself sets its handler
  with `exception()` before declaring routes and calls `HareExceptionHandlers.get_response(error)` first
  in it.
- **A request's own state.** Each request starts with an empty count of `RepeatedQueryDetector` (a
  `before_request` hook). Only its own writes send its reads to the connection written through ([reading
  your own writes](../connections/multiple-databases.md#read-your-writes)) — not the writes of the
  requests its task served before or of the application's startup.
- **A transaction per request** with `atomic_requests` — see below.

A router of the application is a `HareSubRouter` — a `SubRouter` whose routes get the same, and its
transactions once the application includes it:

```python
from hare.contrib.frameworks.robyn import HareSubRouter

chapters = HareSubRouter(prefix="/chapters")


@chapters.get("/:pk", response_model=ChapterSchema)
async def get_chapter(chapter: ChapterQuery) -> Chapter:
    return await chapter.get()


app.include_router(chapters)
```

## <a id="request-queries-for-handlers"></a>Request queries for handlers

A handler's parameter annotated with a request query class (`books: BookQuery`) gets the query built from
the request: its query string — a repeated name for each value of a list,
`?status__in=draft&status__in=published` — and its path parameters, for the parameters marked `InPath()`
(`pk: Annotated[KeyColumns[int, int], InPath()]` with `/chapters/:pk`). Robyn itself would read a
pydantic model from the request's body. The query gets the request (`query.request`) and the request's
whole address for a page's `next` and `previous`. A value the query refuses is answered with `422`.

The OpenAPI schema (`HareOpenAPI`, the application's by default) lists each parameter of the query — a
query parameter, a path parameter for one marked `InPath()` — with its type, limits, enum and
description.

## <a id="responses"></a>Responses

A route's response — a row, a list, a page — is validated with its `response_model` from its attributes
and answered as JSON with the route's `status_code` (200 by default); Robyn itself only validates a
returned dict. A returned `Response` is sent as it is. The schemas of a row, a list and a page — see
[Web frameworks](web-frameworks.md#response-schemas).

## <a id="atomic-requests"></a>A transaction per request: `atomic_requests`

`HareRobyn(..., atomic_requests=True)` runs each handler in a transaction, committed before Robyn sends
the response; a handler marked `RequestTransaction.skip` runs without it, and so does a const route:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(request: Request) -> None: ...
```

When the transaction commits or rolls back — see [Web frameworks](web-frameworks.md#atomic-requests).

## <a id="tenancy"></a>A tenant per request

Robyn runs a request's `before_request` hooks apart from its handler: a scope a synchronous hook sets
with `Tenancy.set()` reaches the handler, one an `async` hook sets doesn't. `HareRobyn` starts every
request with no tenant scope and no query tags — before the application's own hooks — so a request
without a tenant never reads the previous request's rows:

```python
@app.before_request()
def set_tenant(request: Request) -> Request:
    company = request.headers.get("x-company")
    if company:
        Tenancy.set(int(company))
    return request
```

A request the hook gives no tenant queries a tenant-scoped model with no scope and gets `QueryError`. A
handler may also open the scope itself — `with Tenancy.scope(company_id): ...`.

## <a id="health-routes"></a>Health routes: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = HareRobyn(__file__, hare_config=HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))
```

The readiness and liveness routes it adds — see [Web frameworks](web-frameworks.md#health-routes).

## <a id="tests"></a>Tests

Robyn's `TestClient` runs the application in an event loop of its own — with hare's pytest plugin take
`hare_app_transactional_db`: the application takes the test's context, its writes commit, and every table
is emptied when the test ends — see [Application tests](../testing/setup.md#application-tests). It needs a
server database or a SQLite file.
