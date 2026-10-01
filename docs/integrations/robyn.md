# Robyn integration

`hare.contrib.frameworks.robyn` connects Hare to a [Robyn](https://robyn.tech) application.
Install it with the `robyn` extra (Robyn 0.88, pydantic).

```python
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.robyn import HareRobyn
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://app:secret@localhost:5432/app"},
    "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
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

## `HareRobyn` {: #harerobyn }

`HareRobyn(file_object, *, hare_config, atomic_requests=False, openapi=None, **kwargs)` is a `Robyn`
application; `file_object` and `kwargs` are what `Robyn()` takes.

- **The Hare context.** The models are bound when the application is created; the context opens
  when a worker process starts - before the application's own `startup_handler` - and closes when
  it stops, after its own `shutdown_handler`. The context is the global fallback, so every request
  sees it. On shutdown `Hare.close_connections()` also waits for the background observers still
  running.
- **Request queries are checked at startup.** `RequestQuery.check_declarations()` runs once the
  models are set up: a wrong [request query](request-queries.md) stops the process from starting.
- **Request queries for handlers** and their parameters in the OpenAPI schema - see below.
- **Responses validated with the route's `response_model`** - see below.
- **ORM errors as HTTP answers** (`HareExceptionHandlers`):

  | Error | Answer |
  |---|---|
  | `InvalidRequestQuery` | `422 Unprocessable Content` as Robyn answers an invalid pydantic body itself: `{"error": "Validation Error", "detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; an error of the whole query (a model validator's) has `"loc": ["query"]` and the query's parameters as `input` |
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (unique, foreign key, protected relation) | `409 Conflict`, `{"detail": "Conflict"}` - without the database's message, which names tables and constraints |
  | `RequestQueryForbidden` (a parameter asks for what the request may not see: deleted rows without `may_see_deleted()`) | `403 Forbidden`, the reason in `detail` |

  Any other error is left to Robyn (`500`). Robyn keeps one exception handler per application and
  hands it to each route when the route is declared: an application answering errors itself sets
  its handler with `exception()` before declaring routes and calls
  `HareExceptionHandlers.get_response(error)` first in it.
- **A request's own count of repeated queries.** Each request starts with an empty count of
  `RepeatedQueryDetector` (a `before_request` hook).
- **A transaction per request** with `atomic_requests` - see below.

A router of the application is a `HareSubRouter` - a `SubRouter` whose routes get the same, and
its transactions once the application includes it:

```python
chapters = HareSubRouter(prefix="/chapters")


@chapters.get("/:pk", response_model=ChapterSchema)
async def get_chapter(chapter: ChapterQuery) -> Chapter:
    return await chapter.get()


app.include_router(chapters)
```

## Request queries for handlers {: #request-queries-for-handlers }

A handler's parameter annotated with a request query class (`books: BookQuery`) gets the query
built from the request: its query string - a repeated name for each value of a list,
`?status__in=draft&status__in=published` - and its path parameters, for the parameters marked
`InPath()` (`pk: Annotated[KeyColumns[int, int], InPath()]` with `/chapters/:pk`). Robyn itself
would read a pydantic model from the request's body. The query gets the request (`query.request`)
and the request's whole address for a page's `next` and `previous`. A value the query refuses is
answered with `422`.

The OpenAPI schema (`HareOpenAPI`, the application's by default) lists each parameter of the
query - a query parameter, a path parameter for one marked `InPath()` - with its type, limits, enum
and description.

## Responses {: #responses }

A route returns hare models with its `response_model`: a schema of one row with
`from_attributes=True` for a row or a list (`list[BookSchema]`), and for a page of rows the page's
schema - `PageSchema[BookSchema]` (`ScrollPageSchema`, `CursorPageSchema` for the other pages). The route's
response - a row, a list, a page - is validated from its attributes and answered as JSON with the
route's `status_code` (200 by default); Robyn itself only validates a returned dict. A returned
`Response` is sent as it is. The rows of a page are in `result`.

## A transaction per request: `atomic_requests` {: #atomic-requests }

`atomic_requests=True` runs each handler in a transaction on the default connection, like Django's
`ATOMIC_REQUESTS`; a sequence of connection names opens one on each of them. The transaction:

- commits when the handler's response has a status below 500 - before Robyn sends it, so a failed
  commit is still answered with an error;
- rolls back on a response with a status of 500 or more, and on an exception, handled or not;
- runs `Transactions.on_commit()` callbacks after the commit, as usual.

A handler marked `RequestTransaction.skip` runs without it:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(request: Request): ...
```

With several connections the transactions commit one after another; a failure to commit one rolls
back those not committed yet, not those already committed. A name the configuration doesn't have
stops the process from starting.
