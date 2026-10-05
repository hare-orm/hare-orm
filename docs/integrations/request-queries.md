# Request queries

`hare.contrib.request_query` describes the rows a request asks for once, on a class: the queryset
it starts from, the parameters it filters by, the search, the orderings a request may choose and
the pagination. The class is a pydantic model of the request's parameters; a framework adapter
([Litestar](litestar.md), [FastAPI](fastapi.md), [Robyn](robyn.md)) builds it from a request and hands it to the handler, which runs it:

```python
from typing import Annotated

from hare.contrib.request_query import Filter, OffsetPagination, OrderingConfig, RequestQuery, SearchConfig


class BookQuery(RequestQuery[Book]):
    author: int | None = None
    title__icontains: str | None = None
    genre_ids: Annotated[list[int] | None, Filter("genres", lookup="in")] = None

    class Meta:
        queryset = Book.objects.all().select_related("author")
        search = SearchConfig(fields=("title", "author__name"))
        ordering = OrderingConfig(fields=("title", "published_at"), default=("-published_at",))
        pagination = OffsetPagination(default_limit=50, max_limit=200)


page = await BookQuery(author=3, ordering="title").page()
```

Install it with the `request-query` extra (pydantic).

The filters, their lookups and the values they take come from the ORM itself —
[`get_lookup_info()`](../querying/describing-filters.md) — so a lookup a project registers with `register_lookup()`
works like a built-in one, and a declaration that doesn't match the models fails when the
application starts, not on a request.

## <a id="parameters"></a>Parameters

A parameter without a marker filters by its own name: `author` is `.filter(author=...)`,
`title__icontains` is `.filter(title__icontains=...)`. A parameter whose value is `None` doesn't
filter. Every key `.filter()` accepts works:

| Parameter | Filter |
|---|---|
| <code>author: int &#124; None</code> | the relation by the related key (`author=3`) |
| <code>author&#95;&#95;in: list&#91;int&#93; &#124; None</code> | the relation by a list of keys |
| <code>reservation&#95;&#95;work&#95;time&#95;slot&#95;&#95;project&#95;scheme&#95;&#95;in: list&#91;UUID&#93; &#124; None</code> | a relation at the end of a chain |
| <code>reservation&#95;&#95;work&#95;time&#95;slot&#95;&#95;project&#95;scheme&#95;&#95;pk&#95;&#95;in: list&#91;UUID&#93; &#124; None</code> | the same through `pk` |
| <code>profile&#95;&#95;city: str &#124; None</code> | a one-to-one relation, from either side |
| <code>tags&#95;&#95;name: str &#124; None</code> | a many-to-many relation — each row is returned once |
| <code>published&#95;at&#95;&#95;year&#95;&#95;gte: int &#124; None</code> | a date part |
| <code>pk: KeyColumns&#91;int, int&#93; &#124; None</code> | a composite primary key, `?pk=2,1` |
| <code>line: KeyColumns&#91;int, int&#93; &#124; None</code> | a relation to a model with a composite key |
| <code>score&#95;&#95;within: tuple&#91;int, int&#93; &#124; None</code> | a lookup registered with `register_lookup()` |

### <a id="meta-filters"></a>From a list of fields: `Meta.filters`

```python
from hare.contrib.request_query import FilterField
from hare.query.enums import Lookup


class BookQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        filters = (
            FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("author", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("published_at", lookups=(Lookup.GTE, Lookup.LTE, Lookup.RANGE)),
            FilterField("author__profile", lookups=(Lookup.ISNULL,)),
            FilterField("author__profile__city", lookups=(Lookup.EXACT, Lookup.IN), parameter="city"),
        )
```

`FilterField(path, lookups=(Lookup.EXACT,), parameter=None, description=None)` — a field, a
relation, a path through relations, an annotation of `Meta.queryset` — makes a parameter per lookup:
`status`, `status__in`, `author`, `author__in`, `published_at__gte`, `published_at__lte`,
`published_at__range`, `author__profile__isnull`. `Lookup.EXACT` (or `"exact"`) is the path itself,
any other lookup adds `__<lookup>`; a lookup registered with `register_lookup()` goes by its name.
`parameter=` names the parameters in place of the path, so the API doesn't show the model's
structure: `city` and `city__in` filter by `author__profile__city`. `description=` replaces the
field's description in the API schema. A `FilterField` is checked when it is declared: the path and
`parameter` must be identifiers, the lookups a non-empty tuple, each once. The type of each parameter
is the value its filter takes, as the ORM describes it:

| The filter takes | The parameter |
|---|---|
| one value | <code>T &#124; None</code> |
| a list (`in`, `not_in`) | <code>list&#91;T&#93; &#124; None</code> — the parameter repeated, `?status__in=draft&status__in=published` |
| a range (`range`) | <code>tuple&#91;T, T&#93; &#124; None</code> — two values, from and to |
| a composite key | `KeyColumns[...]` in place of `T` |
| a field with an enum (`IntEnumField`, `CharEnumField`), compared by `exact`, `not`, `in`, `not_in` | the enum in place of `T` |

`T` is the value's type: the field's, the related key's for a relation, `int` for a date part,
`bool` for `isnull`. A filter taking a value whose type is only known when the query runs (an
annotation of raw SQL, any JSON value) is a `ConfigurationError` — declare its parameter by hand.
A parameter `Meta.filters` makes that the class declares itself, or that an option adds, is a
`ConfigurationError` too: aliases, filter methods and special types stay declared by hand, next to
`Meta.filters`.

The parameters are added once the models are bound — by `Hare.bind_models()`, which
needs no connection, or `Hare.init()` — when a framework adapter reads the parameters for the
handlers' signatures (`HarePlugin` binds the models itself), when the query is first built, or on
`prepare_parameters()`. A subclass gets the parameters of the `Meta.filters` it inherits.

### <a id="descriptions"></a>Descriptions

A filter parameter without a description of its own is described by its field's `description`,
and a list or a range by how it is written ("Repeat the parameter for each value."); the API
schema shows it. A parameter with `Field(description=...)` keeps its own.

### <a id="filter"></a>Aliases: `Filter`

`Filter` gives the parameter a name of its own, so the API doesn't show — and doesn't change with —
the model's structure:

```python
publisher_ids: Annotated[
    list[UUID] | None,
    Filter("author__publisher", lookup="in"),
] = None
```

`Filter(key)` takes the whole key (`Filter("author__name__iexact")`); `lookup` is appended to it
with `__`.

### <a id="nofilter"></a>Parameters that don't filter: `NoFilter`

```python
mode: Annotated[Mode | None, NoFilter()] = None
```

The handler reads `query.mode`; the query ignores it.

### <a id="inpath"></a>Path parameters: `InPath`

```python
pk: Annotated[KeyColumns[int, int], InPath()]
```

A parameter read from the route's path, not from the query string — every framework adapter reads
and documents it so (Litestar's own `FromPath` works in a Litestar application too).

### <a id="filter-methods"></a>Filter methods

A method `filter_<parameter>(self, value)` turns the parameter's value into a `Q` of its own —
sync or async; `None` adds no condition:

```python
class BookQuery(RequestQuery[Book]):
    rated_at_least: int | None = None

    def filter_rated_at_least(self, value: int) -> Q:
        return Q(author__rating__gte=value)
```

A parameter with a filter method isn't checked against the ORM — its method decides what it does.

The async filter methods of the parameters that have a value run at once (`asyncio.gather`) — a
slow lookup in one doesn't wait for another; the conditions keep the order the parameters are
declared in. Inside a transaction their queries still use the transaction's connection one at a
time.

### <a id="types"></a>Types

A parameter's annotation must take the value its filter takes, as `get_lookup_info()` describes
it:

- one value (`LookupValueShape.VALUE`) — the value's type, a subclass, an enum or a `Literal` of
  such values (`str` for a text lookup, `int` for a relation to an integer key, `bool` for
  `isnull`, `tuple[UUID, int]` for a composite key);
- a list (`LIST`, `__in`) — `list[...]`, `set[...]`, `frozenset[...]` or `tuple[..., ...]` of it;
- a range (`RANGE`, `__range`) — `tuple[T, T]` or `list[T]`.

`bool` doesn't count as `int`. A mismatch is a `ConfigurationError` naming the class, the
parameter, what the filter takes and what the parameter is annotated with.

Three types read one text value:

- `KeyColumns[int, int]` — the values of a composite key joined with commas (`?pk=2,1`); a value
  holding a comma is written `%2C`. A list of keys is `list[KeyColumns[int, int]]` — one
  parameter per key (`?pk__in=2,1&pk__in=3,1`).
- `CommaSeparated[int]` — a list in one parameter (`?ids=1,2,3`); repeated parameters are joined,
  empty items skipped.
- `GenericTarget` — the target of a
  [`GenericForeignKeyField`](../models/relations.md#genericforeignkeyfield) as
  `<branch>:<key>` (`?target=post:1`, a composite key's values joined with commas:
  `article_version:7,2`), read as `{"type": "post", "id": 1}`. `Meta.filters` types
  `FilterField("target")` with it and `FilterField("target__type")` with a `Literal` of the branch
  names.

A value that doesn't fit — of the wrong type, out of range, an ordering the query doesn't allow —
raises `InvalidRequestQuery` with one error per parameter (`loc`, `msg`, `type`); a framework
adapter answers it as that framework answers invalid parameters itself — `400 Bad Request` in
Litestar, `422 Unprocessable Content` in FastAPI and Robyn.

### <a id="bounds"></a>Bounds

The bounds a request gives one field must leave a value between them. Each lower bound (`gt`,
`gte`) is paired with each upper bound (`lt`, `lte`) of the same field path — declared or from
`Meta.filters`, under an alias too (`published_since` with `Filter("published_at", lookup="gte")`),
through a date part as well (`published_at__year__gte`): `?published_at__gte=2026-02-01&published_at__lte=2026-01-01`
is an `InvalidRequestQuery` on the upper bound's parameter (`type` `range`). Equal bounds pass, unless
one of them is strict (`gt`, `lt`). A `range` parameter starting after it ends is refused the same
way. Values that don't compare (an aware and a naive datetime) are left to the database.

### <a id="order-of-the-parameters"></a>Order of the parameters

The parameters the class declares come first, then those of `Meta.filters`, then the options'
(search, ordering, fields, include, deleted rows, pagination) — in the model's fields and in the
API schema.

## <a id="options"></a>Options: `Meta`

| Option | Meaning |
|---|---|
| `queryset` | The rows the query starts from: `Book.objects.all().select_related("author")`. A class without one only serves as a base. |
| `filters` | A tuple of `FilterField` — the fields a request may filter by and each one's lookups; see [`Meta.filters`](#meta-filters). |
| `search` | A `SearchConfig`, None for no search. |
| `ordering` | An `OrderingConfig`, None when a request can't choose the ordering. |
| `pagination` | An `OffsetPagination`, a `ScrollPagination` or a `CursorPagination`, None for none. `OffsetPagination()` by default. |
| `join_type` | How the parameters' conditions join — `Connector.AND` (default) or `Connector.OR`. |
| `fields` | A `FieldsConfig`, None when a request can't choose the fields to load. |
| `include` | An `IncludeConfig`, None when a request can't ask for relations. |
| `deleted` | A `DeletedConfig`, None when a request can't see soft-deleted rows. |
| `versions` | `LatestVersions()` to keep only the latest version of each record of a `VersionedModel`, None for every version. |

A subclass inherits each option it doesn't set from the nearest base that does.

`queryset` is built when the module is imported, before `Hare.init()`; each request works on a
copy. `get_queryset()` can be overridden for rows that depend on the request — the filters are
still checked against `Meta.queryset`, so a filter on an annotation needs the annotation there.
A model with `Meta.tenant_field` needs nothing more: the scope of `Tenancy.scope()` applies when
the query runs, not when `Meta.queryset` is built.

Each option that adds parameters (`search`, `ordering`, `fields`, `include`, `deleted`,
`pagination`) is a `RequestOption`; `parameter=` (`limit_parameter=`, `offset_parameter=`,
`cursor_parameter=` for a pagination) renames its query parameter. A `RequestOption` names its
parameters (`get_parameter_fields()`), checks the values a request gives them (`check_request()`)
and applies them — an option of your own derives from it the same way.

### <a id="searchconfig"></a>Search: `SearchConfig`

```python
search = SearchConfig(fields=("title", "author__name"), lookup="icontains", parameter="search", join_type=Connector.OR)
```

`?search=tolk` matches rows where any field (`Connector.OR`) or every field (`Connector.AND`) matches. Each field's
lookup must take text. A blank search filters nothing.

`split_words=True` searches word by word: every word of the text must match, each in the
fields joined by `join_type` — `?search=ursula wizard` finds a book by Ursula titled "Wizard".

### <a id="orderingconfig"></a>Ordering: `OrderingConfig`

```python
ordering = OrderingConfig(fields=("title", "published_at", "author__name"), default=("-published_at",))
```

A request names one or more of `fields`, comma-separated, `-` for descending:
`?ordering=-published_at,title`. A name outside `fields` or named twice is refused. The rows are
ordered by the handler's `order_by()`, else the request's ordering, else `default`, else the
queryset's own ordering, else the model's `Meta.ordering` — and then by the primary key's fields
not ordered by yet, so rows with equal values keep one order from page to page (every field of a
composite key); a model without a primary key (`Meta.primary_key = None`) gets no such fields. An
ordering through a relation to many rows is refused — each row would repeat.

### <a id="pagination"></a>Pagination

Every pagination derives from `Pagination` — the page size a request may ask for
(`default_limit`, `max_limit`) and its parameter (`limit_parameter`); each adds how a request
says which page and builds its own type of page (`get_page()`), so a pagination of your own is a
subclass too.

`OffsetPagination(default_limit=100, max_limit=1000, limit_parameter="limit", offset_parameter="offset")`
pages by `?limit=` and `?offset=`. `page()` returns a `Page`: `result`, `count` (all matching
rows), `limit`, `offset`, `next` and `previous` — the request's address with `offset` replaced,
every other parameter kept as it was, repeated ones included; `previous` of an offset smaller than
the page size leads to the first page.

`ScrollPagination(default_limit=100, max_limit=1000, limit_parameter="limit", offset_parameter="offset")`
pages by `?limit=` and `?offset=` as well, but without counting the matching rows — for a feed or
a long list that shows only "next" and "previous": the `COUNT` of a large table is saved, and
whether a next page exists is read from one row fetched past the page. `page()` returns a
`ScrollPage`: `result`, `limit`, `offset`, `next`, `previous` — it has no count at all. Both offset
paginations derive from `BaseOffsetPagination`.

`CursorPagination(default_limit=100, max_limit=1000, limit_parameter="limit", cursor_parameter="cursor")`
pages by a cursor: the ordering values of the row a page starts after or ends before
(`after_cursor()`/`before_cursor()`). A page costs the same wherever it is, rows added between
requests don't shift the pages, and there is no count. `page()` returns a `CursorPage`: `result`,
`limit`, `next_cursor`, `previous_cursor`, `next`, `previous`. A cursor names the ordering it was
made for; one of another ordering, or text that isn't a cursor, is an `InvalidRequestQuery`. The
ordering of a cursor pagination may not use an annotation; fields through forward relations are
loaded with the rows (`select_related()`), since the cursor reads them off the last row. A model
without a primary key can't have a cursor pagination: rows equal in the ordering would be skipped.

Page sizes are checked when the option is created: `1 <= default_limit <= max_limit <= 100000`.

### <a id="fields-and-include"></a>Fields and relations: `FieldsConfig`, `IncludeConfig`

```python
fields = FieldsConfig(fields=("title", "status", "published_at"))
include = IncludeConfig(relations=("author", "author__profile", "tags"))
```

`?fields=title,status` loads only the named fields (`QuerySet.only()`), the primary key and
the fields the ordering reads; reading another field of such a row raises `AttributeError`.
The names must be the model's own fields — relations are loaded with `include`.
`?include=author,tags` loads the named relations with the rows: a relation to one row with a
join (`select_related()`), a relation to many rows with a query of its own
(`prefetch_related()`). A name the option doesn't list is an `InvalidRequestQuery`. Both apply
to `fetch()`, `page()` and `get()`.

### <a id="deletedconfig"></a>Deleted rows: `DeletedConfig`

```python
deleted = DeletedConfig(parameter="deleted")
```

For a model with `Meta.soft_delete_field`: `?deleted=include` — every row, `?deleted=only` — the
deleted ones, `?deleted=exclude` or no value — the rest, as without the option. Asking for deleted
rows needs `may_see_deleted()` to allow it — otherwise the query raises `RequestQueryForbidden`
and a framework adapter answers `403 Forbidden`:

```python
class ProjectBookQuery(BookQuery):
    async def may_see_deleted(self) -> bool:
        return self.request.user.is_admin
```

`may_see_deleted()` returns True by default: declaring the option lets every request see them. A
model without `Meta.soft_delete_field` is a `ConfigurationError`.

### <a id="latestversions"></a>Latest versions: `LatestVersions`

```python
class ArticleQuery(RequestQuery[Article]):
    class Meta:
        queryset = Article.objects.filter(status="published")
        versions = LatestVersions()
```

For a `VersionedModel`: only the latest version of each record — the rows for which no newer
version of the same `id` exists, one correlated subquery (`NOT EXISTS`), not a list of ids. The
versions compared are the rows of `Meta.queryset` (above: the latest published version) with the
deleted rows as the request asks for them and the access condition — a newer version the request
may not see doesn't hide the one it may. The request's filters apply to the latest versions: a
filter matching only an older version finds nothing. A model that isn't a `VersionedModel` is a
`ConfigurationError`.

## <a id="running-a-query"></a>Running a query

| Method | Returns |
|---|---|
| `await query.page()` | A `Page`, a `ScrollPage` or a `CursorPage`, by `Meta.pagination`. |
| `await query.fetch()` | Every matching row, ordered, without pagination. |
| `await query.get()` | The one matching row; `DoesNotExist` / `MultipleObjectsReturned` otherwise. Takes `does_not_exist_exception` and `multiple_objects_returned_exception` as [`QuerySet.get()`](../querying/queryset-methods.md) does — `get(does_not_exist_exception=None)` gives None when no row matches. |
| `await query.count()` / `exists()` | How many rows match / whether any does. |
| `await query.delete()` | Deletes the matching rows and returns how many. |
| `await query.update(**values)` | Updates the matching rows and returns how many. |
| `await query.get_for_update()` | The one matching row, locked until the transaction ends. |
| `await query.count_by(*names, limit=None)` | How many matching rows have each value of each field. |
| `await query.get_filtered_queryset()` | The filtered queryset, not ordered — for anything else. |

A condition across a relation to many rows (a reverse FK, a many-to-many relation) makes the query
`DISTINCT`, so each row is returned and counted once — any condition: a parameter, a filter
method's `Q`, the search, the handler's `where()`, `get_access_condition()`.

`delete()` and `update()` need a filter from the request or from `where()` — a request
filtering by nothing raises `QueryError` instead of changing every row the user may see.

`get_for_update()` is for a handler that changes the row: call it inside a transaction; it
locks the row with `SELECT ... FOR UPDATE` where the database locks rows (SQLite serializes
writers instead). The fields and include parameters don't apply to it.

### <a id="count-by"></a>Counts by value: `count_by()`

```python
counts = await query.count_by("status", "author", "tags")
# {"status": {"published": 3, "draft": 2}, "author": {1: 2, 2: 2, 3: 1}, "tags": {1: 3, 2: 2, None: 1}}
```

For the filters of a list, which show how many rows each choice leaves. Each field is counted
under every filter of the request except its own — `?status=draft` still counts every status —
with the search, `where()` and the access condition. Every key of a relation is its own filter
(`author`, `author__in`, `author_id__in`), and so is a date part of a field
(`published_at__year`). A relation counts by the related key — a tuple for a composite key; a
relation to many rows counts a row once per related row; `None` counts the rows without a
value. The most frequent values come first; `limit=` keeps that many. A filter across a relation
to many rows counts each row once — which needs a primary key: on a model without one it
raises `QueryError`.

### <a id="the-handlers-part"></a>The handler's part

- `query.where(*conditions, **filters)` adds conditions of the handler's own, joined with `AND`.
- `query.order_by(*names)` replaces the ordering — names or ordering expressions
  (`Ordering("rating", Order.DESC_NULLS_LAST)`).
- `query.select_related(...)`, `prefetch_related(...)`, `annotate(...)` change the queryset.
- `query.request` is the request the query was built from; `query.cache_key()` names the class and
  its parameter values — the same for the same request — for caching a result together with
  whatever else it depends on.

### <a id="hooks"></a>Hooks

```python
class ProjectBookQuery(BookQuery):
    async def get_access_condition(self) -> Q | None:
        return Q(owner=self.request.user.id)

    async def after_fetch(self, items: list[Book]) -> list[Book]:
        await attach_prices(items)
        return items
```

`get_access_condition()` returns the rows the request may see at all — it joins every query,
`delete()` included. `after_fetch()` runs on the rows of `fetch()`, `page()` and `get()`.
`may_see_deleted()` decides whether the request may ask for deleted rows (see `DeletedConfig`).

## <a id="without-a-web-framework"></a>Without a web framework

```python
query = BookQuery.from_query_string("status=draft&tag_ids=1&tag_ids=2&ordering=-title")
query = BookQuery.from_query_parameters([("status", "draft"), ("tag_ids", "1")], id=3)
```

Builds a query from a URL's query string or from `(name, value)` pairs — for tests, a CLI, a
WebSocket message or a framework without an adapter. A parameter taking a list gets every value
of a repeated name, any other the last one; a name the query doesn't have is ignored; keyword
values (path parameters) win over the query string; `request=` passes the request.
`query.set_request_url(url)` sets the request's whole address for the links of a page when
the request's `url` isn't it.

## <a id="dialects"></a>Dialects

`RequestQuery` checks every filter and search lookup against each dialect a driver connects to, so
it can't use a lookup only one of them runs. A dialect's own request query — `PostgresqlRequestQuery`
of `hare.dialects.postgresql.request_query`, `SqliteRequestQuery` of
`hare.dialects.sqlite.request_query` — checks against that dialect only, and requires its model's
connection to be of it:

```python
from hare.contrib.request_query import SearchConfig
from hare.dialects.postgresql.request_query import PostgresqlRequestQuery


class ArticleQuery(PostgresqlRequestQuery[Article]):
    tags__overlap: list[str] | None = None

    class Meta:
        queryset = Article.objects.all()
        search = SearchConfig(fields=("title", "body"), lookup="search")
```

A PostgreSQL request query may use full-text `search`, the `trigram_*` lookups, array, range and
JSON container lookups, and lookups registered for PostgreSQL only. A lookup that needs an
extension (`pg_trgm`) needs it installed in the database.

## <a id="checking-declarations"></a>Checking declarations

`RequestQuery.check_declarations()` checks every request query class whose queryset's model is
registered in the current Hare context, and raises one `ConfigurationError` listing every wrong
one: a parameter naming no filter of the model, a filter a dialect doesn't run, an annotation that
doesn't take the filter's value, a search field that doesn't take text, an ordering the model lacks
or that crosses a relation to many rows, a cursor ordering by an annotation, a `Meta` option of the
wrong type. A class of a model the context doesn't have belongs to another application and is
skipped. Every framework adapter ([Litestar](litestar.md), [FastAPI](fastapi.md), [Robyn](robyn.md))
calls it at startup; without one, a class is checked on its first use (`get_declaration()`). The declaration holds the ORM's descriptions of the class's
filters and orderings — an ordering the class meets later (the queryset's own, a handler's
`order_by()`) is described the first time — so a request reads none of them again.

## <a id="live-models"></a>Models registered while the application runs

A model registered with `Hare.register_live_models()` — a content type's table, a table opened in
a database browser — gets its request queries the same way. `RequestQuery.for_model()` builds a
class for it, checked at once:

```python
from hare.contrib.request_query import FilterField, OrderingConfig, RequestQuery
from hare.query.enums import Lookup

ContentQuery = RequestQuery.for_model(
    content_model,
    filters=(FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),),
    ordering=OrderingConfig(fields=("title",), default=("title",)),
)
page = await ContentQuery.from_query_string("status__in=draft&status__in=published").page()
```

It takes each `Meta` option as a keyword argument — `queryset` (`model.objects` by default),
`filters`, `search`, `ordering`, `pagination` (an `OffsetPagination()` by default), `fields`,
`include`, `deleted`, `versions`, `join_type` — and `name`, the class's name
(`<Model>RequestQuery` by default). A wrong declaration raises `ConfigurationError` right there,
as `check_declarations()` would find it. Called on a dialect's request query
(`PostgresqlRequestQuery.for_model()`), the class is of that dialect. The class works wherever a
declared one does — `RequestQueryDIPlugin.provide()`, `RequestQueryDependency.provide()`, a Robyn
handler's parameter, `PageSchema[...]`, `HareDTO` — and is held weakly: once nothing holds it, it
leaves the declarations and `get_concrete_subclasses()`.

A request query class forgets its declaration and its parameters with every model they read — its
queryset's model, and each model its filters, search, orderings, `fields` or `include` cross. That
happens when such a model is unregistered or registered again (`Hare.unregister_live_models()`,
`register_live_models()`), when a relation is added to it, and after any registration in `Registries`
(a lookup, a renderer, a type mapping, a QuerySet method, a driver). The next use builds them again
from the models as they are now: a parameter of `Meta.filters` gets the type its field has now, a
field that is gone is a `ConfigurationError`. A class whose `Meta.queryset` is of an unregistered
model refuses to run with a `ConfigurationError` — build the class again for the model registered
in its place. `check_declaration()` rebuilds and checks one class.

## <a id="describe-parameters"></a>Describing the parameters: `describe_parameters()`

`describe_parameters()` says what each parameter is — what a filter panel, a relation picker or a
sort menu needs beyond the JSON schema. It needs the models bound, not the connections, so it works
after `Hare.bind_models()`:

```python
for parameter in BookQuery.describe_parameters():
    print(parameter.name, parameter.parameter_type, parameter.filter_key, parameter.relation)
```

Each `ParameterDescription` has:

| Attribute | What it is |
|---|---|
| `name`, `parameter_type` | The parameter and what it does — `ParameterType.FILTER`, `FILTER_METHOD` (a `filter_<name>` method), `NO_FILTER`, `SEARCH`, `ORDERING`, `LIMIT`, `OFFSET`, `CURSOR`, `FIELDS`, `INCLUDE`, `DELETED`, `OPTION` (an option of your own). |
| `annotation`, `description`, `default`, `required` | Its type, its description, its default. |
| `takes_many_values` | It takes every value of a repeated parameter, or a `CommaSeparated` list — a multi-select. |
| `in_path` | It is read from the route's path (`InPath()`). |
| `choices` | The members of the enum it takes, as `ParameterChoice(value, label)` — the label is the member's name. |
| `allowed_values` | For an option's parameter, the names a request may give: the orderings, the `fields`, the `include` relations, the `deleted` modes. |
| `filter_key`, `path`, `lookup` | A filter's `.filter()` key, the key without its lookup, and the lookup. |
| `value_shape`, `value_type` | Whether a filter takes one value, a list or a range, and the type of a value — a tuple of types for a composite key. |
| `nullable` | The value the filter compares can be missing — a nullable field, a nullable or to-many relation — so an "empty" choice means something. |
| `relation` | The last relation a filter crosses, as `RelationDescription(path, model, key_fields, to_many, compares_key)` — `compares_key` when the value is the related row's key (`author`, `tags__in`), a relation picker; `key_fields` has every field of a composite key. |
| `bound`, `paired_parameters` | Which bound of its path a filter gives (`BoundSide.LOWER`, `UPPER`, `RANGE`) and the parameters giving the other one (`published_at__gte` pairs with `published_at__lte`). |

A class forgotten with its models describes them as they are now. Fields have no choices of their
own besides an enum: a field of `CharEnumField`/`IntEnumField` gives its enum's members.
