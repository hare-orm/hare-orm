# FastAPI integration

`hare.contrib.frameworks.fastapi` connects Hare to a [FastAPI](https://fastapi.tiangolo.com)
application. Install it with the `fastapi` extra (FastAPI 0.135 or newer, pydantic).

```python
from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://app:secret@localhost:5432/app"},
    "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
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

## `HareFastAPI` {: #harefastapi }

`HareFastAPI(*, hare_config, atomic_requests=False, lifespan=None, **kwargs)` is a `FastAPI`
application; `kwargs` are what `FastAPI()` takes.

- **The models are bound when the application is created** (`Hare.bind_models()`, no
  connection). FastAPI reads a route's parameters when the route is declared, and a request
  query's parameters - [`Meta.filters`](request-queries.md#meta-filters), the
  descriptions of its filters - are read from the models. A router declared in a module imported
  before the application is created needs `Hare.bind_models(HARE_CONFIG)` called
  before that import.
- **The Hare context.** Opened when the application starts, before its own `lifespan` - which
  already has the database - and closed when it stops. The context is the global fallback, so
  every request sees it. On shutdown `Hare.close_connections()` also waits for the background
  observers still running.
- **Request queries are checked at startup.** `RequestQuery.check_declarations()` runs once the
  models are set up: a wrong [request query](request-queries.md) stops the application from starting.
- **ORM errors as HTTP answers**, in FastAPI's own error format, unless the application answers the
  error itself (`exception_handlers=`):

  | Error | Answer |
  |---|---|
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (unique, foreign key, protected relation) | `409 Conflict`, `{"detail": "Conflict"}` - without the database's message, which names tables and constraints |
  | `InvalidRequestQuery` | `422 Unprocessable Content` as FastAPI answers invalid parameters itself: `{"detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; an error of the whole query (a model validator's) has `"loc": ["query"]` and the query's parameters as `input` |
  | `RequestQueryForbidden` (a parameter asks for what the request may not see: deleted rows without `may_see_deleted()`) | `403 Forbidden`, the reason in `detail` |

- **A request's own count of repeated queries.** Each request starts with an empty count of
  `RepeatedQueryDetector`.
- **A transaction per request** with `atomic_requests` - see below.

## Request queries as dependencies {: #request-queries-as-dependencies }

`RequestQueryDependency.provide(QueryClass)` is the `Depends()` building a request query from the
request. Each parameter of the class is a parameter of the route, validated by FastAPI and listed
in the OpenAPI schema with its type, default, limits, enum and description:

- a query parameter - a list one repeated, `?status__in=draft&status__in=published`;
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

`KeyColumns` and `CommaSeparated` reach the query as the text of the parameter, which the query
parses. The query gets the request: `query.request`, and the addresses of a page's `next` and
`previous`. A value the query refuses - an ordering it doesn't allow, a malformed cursor - is
answered as FastAPI answers an invalid parameter.

## Responses {: #responses }

A route returns hare models with its `response_model`: a schema of one row with
`from_attributes=True` for a row or a list (`list[BookSchema]`), and for a page of rows the page's
schema - `PageSchema[BookSchema]` (`ScrollPageSchema`, `CursorPageSchema` for the other pages). FastAPI validates
what the route returns from its attributes and documents it. The rows of a page are in `result`.

## A transaction per request: `atomic_requests` {: #atomic-requests }

`atomic_requests=True` runs each HTTP request in a transaction on the default connection, like
Django's `ATOMIC_REQUESTS`; a sequence of connection names opens one on each of them. The
transaction:

- commits when the response starts with a status below 500 - before the response is sent, so a
  failed commit is still answered with an error; the body of a streaming response is sent after the
  commit;
- rolls back on a response with a status of 500 or more, and on an exception, handled or not;
- runs `Transactions.on_commit()` callbacks after the commit, as usual.

A route whose endpoint is marked `RequestTransaction.skip` runs without it:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(...): ...
```

WebSocket connections never run in one. With several connections the transactions commit one
after another; a failure to commit one rolls back those not committed yet, not those already
committed. A name the configuration doesn't have stops the application from starting.
