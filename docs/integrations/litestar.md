# Litestar

`hare.contrib.frameworks.litestar` connects hare to a [Litestar](https://litestar.dev) application.
Install it with the `litestar` extra (Litestar 2.23 or newer, pydantic).

```python
from litestar import Litestar, get
from litestar.di import NamedDependency

from hare.contrib.frameworks.litestar import HarePlugin, RequestQueryDIPlugin
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://library:secret@localhost:5432/library"},
    "apps": {"library": {"models": ["library.models"], "default_connection": "default"}},
}


@get("/books", dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
    return await books.page()


app = Litestar([list_books], plugins=[HarePlugin(HARE_CONFIG, atomic_requests=True)])
```

## <a id="hareplugin"></a>`HarePlugin`

```python
class HarePlugin:
    def __init__(
        self,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        atomic_requests: bool | Sequence[str] = False,
        health_routes: HealthRoutes | None = None,
    ) -> None: ...
```

The plugin gives the application:

- **The `HareContext`.** Opened with the application and closed with it, as the first lifespan manager —
  the application's own lifespan managers (a message broker, an outbox relay) already have the database,
  and stop before it closes. The context is the global fallback, so every request sees it. On shutdown
  `Hare.close_connections()` also waits for the background observers still running.
- **Request queries are checked at startup.** `RequestQuery.check_declarations()` runs once the models
  are set up: a wrong [request query](request-queries.md) stops the application from starting, with every
  wrong class listed.
- **Request queries as dependencies.** `HarePlugin` is a `RequestQueryDIPlugin` itself: an application's
  plugins come before the ones Litestar adds, so it takes a request query before Litestar's own pydantic
  DI plugin would take it as a plain pydantic model.
- **hare models in responses and request data** — `HarePlugin` gives a handler returning or taking them a
  [`HareDTO`](#haredto).
- **ORM errors as HTTP answers**, in Litestar's error format, unless the application answers the error
  itself:

  | Error | Answer |
  |---|---|
  | `DoesNotExist` | `404 Not Found` |
  | `IntegrityError` (unique, foreign key, protected relation) | `409 Conflict`, without the database's message, which names tables and constraints |
  | `TransactionRetryError` (a serialization failure or deadlock — the request may be sent again) | `409 Conflict` |
  | `ValidationError` (a value a field refuses: out of its range, too long, a null byte PostgreSQL refuses) | `422 Unprocessable Content`, the field's message in `detail` |
  | `InvalidRequestQuery` | `400 Bad Request` as Litestar answers invalid parameters itself: `{"status_code", "detail", "extra"}`, each error in `extra` as `{"message", "key", "source"}` (`query` or `path`); an error of the whole query (a model validator's) has no `key` |
  | `RequestQueryForbidden` (a parameter asks for what the request may not see: deleted rows without `may_see_deleted()`) | `403 Forbidden`, the parameter under `extra` |

- **A request's own state.** Each request starts with an empty count of `RepeatedQueryDetector`, so it
  reports the queries of one request. Only its own writes send its reads to the connection written
  through ([reading your own writes](../connections/multiple-databases.md#read-your-writes)) — not the
  writes of the requests its task served before or of the application's startup.
- **A transaction per request** with `atomic_requests` — see below.

`config` is what `Hare.init(config=...)` takes.

## <a id="request-queries-as-dependencies"></a>Request queries as dependencies

`RequestQueryDIPlugin.provide(QueryClass)` is the dependency building a request query from the request.
Each parameter of the class is a parameter of the handler, validated by Litestar and listed in the
OpenAPI schema with its type, default, limits (`limit`'s maximum is the pagination's `max_limit`) and
description. A parameter is a query parameter unless its annotation says otherwise:

```python
class ChapterQuery(RequestQuery[Chapter]):
    pk: FromPath[KeyColumns[int, int]]

    class Meta:
        queryset = Chapter.objects.all()
        pagination = None


@get("/chapters/{pk:str}", dependencies={"chapter": RequestQueryDIPlugin.provide(ChapterQuery)})
async def get_chapter(chapter: NamedDependency[ChapterQuery]) -> ChapterSchema: ...
```

`KeyColumns` and `CommaSeparated` reach the query as the text of the parameter, which the query parses.
The query gets the request: `query.request`, and the addresses of a page's `next` and `previous`.

Litestar builds the handlers' signatures when the application is created, before its lifespan opens the
connections. `HarePlugin` binds the models of its configuration then (`Hare.bind_models()`), so the
parameters of [`Meta.filters`](request-queries.md#meta-filters) are in the signatures and the schema;
with `RequestQueryDIPlugin` alone, call `Hare.bind_models()` before creating the application.

Without `HarePlugin`, list `RequestQueryDIPlugin()` in `plugins`.

## <a id="haredto"></a>Responses: `HareDTO`

A handler returns hare models — a row, a list of rows, a page of rows (`Page[Book]`) — and `HarePlugin`
gives it a `HareDTO` of the model, Litestar's own way of shaping a response: the rows are encoded without
a schema of their own, and the OpenAPI schema describes them.

```python
@get("/books", dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
    return await books.page()
```

The fields of a `HareDTO` are the model's fields:

- each value, typed by the field (its enum for an enum field), `null` for a nullable one — and for a
  field the query left out (`only()`, a request query's `fields`), not the field's default;
- a relation's key column (`author_id`);
- each relation (`author`, `tags`, a reverse relation) as the related rows — `null` unless the query
  loaded them with `select_related()`/`prefetch_related()` or a request query's `include`, so encoding a
  response never runs a query. Loaded rows go one level deep (`DTOConfig.max_nested_depth`, 1 by
  default): without their own relations.

A page keeps its own fields (`count`, `limit`, `next` ...) and has its rows in `result`.

The plugin builds one DTO per model and forgets it with the model's caches: a live model unregistered and
registered again (`Hare.register_live_models()`) gets a DTO of its own, and the unregistered class isn't
kept.

A DTO of its own shapes the response as for any Litestar DTO:

```python
from litestar.dto import DTOConfig

from hare.contrib.frameworks.litestar import HareDTO


class BookDTO(HareDTO[Book]):
    config = DTOConfig(exclude={"tags"}, rename_strategy="camel")


@get("/books", return_dto=BookDTO, dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
    return await books.page()
```

### <a id="related-rows"></a>Related rows

The handler loads the relations it answers with; nothing else is needed:

```python
@get("/books/{book_id:int}")
async def get_book(book_id: FromPath[int]) -> Book:
    return await Book.objects.all().select_related("author").prefetch_related("tags").get(id=book_id)
# {"id": 1, "title": "...", "author_id": 3, "author": {"id": 3, "name": "Anna"},
#  "tags": [{"id": 1, "name": "fantasy"}], "lines": null}
```

A relation to one row is the related row, a relation to many rows (a reverse foreign key, a many-to-many
relation) the list of them, and a relation the query didn't load is `null`. The related rows go as deep
as `DTOConfig.max_nested_depth` — 1 by default, the related rows without their own relations; the
relations of related rows need a DTO of its own and the query loading them:

```python
class BookDetailDTO(HareDTO[Book]):
    config = DTOConfig(max_nested_depth=2)


@get("/books/{book_id:int}/detail", return_dto=BookDetailDTO)
async def get_book_detail(book_id: FromPath[int]) -> Book:
    return await Book.objects.all().select_related("author__profile").prefetch_related("author__books").get(id=book_id)
```

A request query loads the relations a request names with its [`include`](request-queries.md) option
(`?include=author,tags`).

`DTOConfig`'s `exclude`, `include` and `rename_fields` name a field of related rows by its path through
the relations — `author.name`, `tags.id`, `author.books.title`:

```python
class BookDTO(HareDTO[Book]):
    config = DTOConfig(
        max_nested_depth=2,
        exclude={"author.books", "tags.id"},
        rename_fields={"author.name": "authorName"},
    )
```

A `sensitive=True` field (an `EncryptedTextField` too) is in the DTO like any other field — leave it out
with `exclude`, as `exclude_sensitive=True` does for a [Pydantic model](pydantic.md#exclude-sensitive).

A handler taking a model (`data: Book`) gets a `HareDTO` of it too: the request's data makes the row.
Relations, the values the database generates (an autoincrement key) and the `auto_now`/`auto_now_add`
stamps the ORM fills on save are read-only — the data sets the key column (`author_id`), not the
relation, and a stamp sent in the body is ignored.

A change of some fields only (`PATCH`) takes a DTO with `DTOConfig(partial=True)` and `DTOData`: the
model built from partial data has the defaults in the fields not sent, `update_instance()` sets only the
sent ones on the row read from the database:

```python
class PartialBookDTO(HareDTO[Book]):
    config = DTOConfig(partial=True)

@patch("/books/{book_id:int}", dto=PartialBookDTO)
async def change_book(book_id: int, data: DTOData[Book]) -> Book:
    book = data.update_instance(await Book.objects.get(id=book_id))
    await book.save()
    return book
```

## <a id="atomic-requests"></a>A transaction per request: `atomic_requests`

`HarePlugin(..., atomic_requests=True)` runs each HTTP request in a transaction; a route handler with
`opt={SKIP_TRANSACTION_OPT_KEY: True}` runs without it:

```python
from litestar import post

from hare.contrib.frameworks.litestar import SKIP_TRANSACTION_OPT_KEY


@post("/imports", opt={SKIP_TRANSACTION_OPT_KEY: True})
async def import_books() -> None: ...
```

When the transaction commits or rolls back, and what else runs without it — see
[Web frameworks](web-frameworks.md#atomic-requests).

## <a id="health-routes"></a>Health routes: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = Litestar(route_handlers, plugins=[HarePlugin(HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))])
```

The readiness and liveness routes it adds — see [Web frameworks](web-frameworks.md#health-routes).
