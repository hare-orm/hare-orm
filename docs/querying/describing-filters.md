# Describing filters and orderings

`Model._meta.get_lookup_info(key)` tells what a `.filter()` key refers to — the relations it
crosses, the field it compares, the lookup and the value the lookup takes — without building or
running a query. `get_ordering_info(name)` does the same for an `.order_by()` name, and
`get_lookups(path, dialect)` lists every lookup of a field that a dialect runs.

It is meant for code that turns outside input into filters: an API layer checking the filters it
declares at startup and parsing each query parameter into the right type, a form builder, an admin.
Such code doesn't keep its own copy of hare's lookup lists — it asks the model.

hare uses the same descriptions itself: `.filter()`, `.exclude()` and `.order_by()` check every
key through them when called, and a query checks each lookup against its connection's dialect
before building SQL.

## <a id="get_lookup_info"></a>`get_lookup_info()`

```python
from hare.query.enums import Lookup, LookupValueShape

info = Event._meta.get_lookup_info("tournament__name__icontains")
info.relations      # (Event.tournament,)
info.field          # Tournament.name
info.lookup         # Lookup.ICONTAINS
info.value_shape    # LookupValueShape.VALUE
info.value_type     # str
```

It accepts every key `.filter()` accepts:

| Key | `relations` | `field` | `lookup` | `value_shape` | `value_type` |
|---|---|---|---|---|---|
| `name`, `name__icontains` | `()` | `name` | `EXACT`, `ICONTAINS` | `VALUE` | `str` |
| `id__in` | `()` | `id` | `IN` | `LIST` | `int` |
| `id__range` | `()` | `id` | `RANGE` | `RANGE` | `int` |
| `reporter_id__isnull` | `()` | `reporter_id` | `ISNULL` | `VALUE` | `bool` |
| `tournament`, `tournament__in` (a key or an object) | `(tournament,)` | `Tournament.id` | `EXACT`, `IN` | `VALUE`, `LIST` | `int` |
| `tournament_id__gte` | `()` | `tournament_id` | `GTE` | `VALUE` | `int` |
| `tournament__pk`, `tournament__id__in` | `(tournament,)` | `Tournament.id` | `EXACT`, `IN` | `VALUE`, `LIST` | `int` |
| `participants__in` (many-to-many) | `(participants,)` | `Team.id` | `IN` | `LIST` | `int` |
| `events__isnull` (reverse FK) | `(events,)` | `Event.event_id` (Event's primary key) | `ISNULL` | `VALUE` | `bool` |
| `pk` of a composite primary key | `()` | `(id, version)` | `EXACT` | `VALUE` | `(UUID, int)` |
| `pk__in` of a composite primary key | `()` | `(id, version)` | `IN` | `LIST` | `(UUID, int)` |
| `document` — a FK to a composite key | `(document,)` | `(Document.id, Document.version)` | `EXACT` | `VALUE` | `(UUID, int)` |
| `created__year__gte` | `()` | `created` | `GTE` | `VALUE` | `int` |
| `created__date` | `()` | `created` | `EXACT` | `VALUE` | `date` |
| `data__owner__name__icontains` (JSON) | `()` | `data` | `ICONTAINS` | `VALUE` | `str` |
| `data__has_keys` | `()` | `data` | `HAS_KEYS` | `LIST` | `str` |
| `tags__contains` (array) | `()` | `tags` | `CONTAINS` | `LIST` | the element type |
| `tags__len__gt` | `()` | `tags` | `GT` | `VALUE` | `int` |
| `during__overlap` (range) | `()` | `during` | `OVERLAP` | `RANGE` | the bound type |
| a generated field | `()` | the `GeneratedField` | | | its `output_field`'s type |
| `name__<custom>` | `()` | `name` | the lookup's name | as registered | as registered |

A lookup on a relation itself (`tournament=`, `tournament__in=`, `tags__isnull=`) compares the
related model's key, so `field` is that key field — the relation's target field(s) for a forward
FK/O2O, the related primary key for a reverse relation or a many-to-many one — and `relations`
ends with the relation.

### <a id="lookupinfo"></a>`LookupInfo`

An immutable dataclass. Descriptions are built once and cached:

- a model's own filter keys, ordering names (`OrderingInfo`) and `get_lookups()` results per model
  - `get_lookups()` per path and dialect;
- a key or name starting with an annotation per queryset, together with the annotations' output
  fields — a clone shares them, and `annotate()`/`alias()` replacing an annotation drops them;
- a change to any model's fields, lookups or relations (a live model registered or unregistered,
  a field added or removed) drops every cached description.

A key or name that fails raises its `FieldError` again on every call — errors aren't cached.

| Attribute | Meaning |
|---|---|
| `key` | The filter key. |
| `model` | The model the key starts at. |
| `relations` | The relations the key crosses, in order — forward FK/O2O, reverse FK/O2O and many-to-many fields. |
| `field` | The field the value is compared with; a tuple of fields for a composite key; `None` for an annotation whose type is only known once the query runs. |
| `transforms` | The path read inside the field's value before the lookup: a date part (`("year",)`), `date`/`time`, a JSON key path (`("owner", "name")`), an array index, slice or `len`, a range bound or flag, `unaccent`, an hstore key. |
| `lookup` | A `Lookup` member, or the name of a lookup registered with `register_lookup()`. |
| `value_shape` | `LookupValueShape.VALUE` (one value), `LIST` (a list: `in`, `not_in`, `has_keys`, an array's `contains`...) or `RANGE` (two items: `range`, a range field's `overlap`...). |
| `value_type` | The type of the value — of each item of a list or range. A tuple of types for a composite key; `bool` for `isnull`/`not_isnull`; `int` for a date part or `len`; `str` for a text lookup or a JSON/hstore key; `object` for any JSON value. |
| `crosses_to_many` | Whether a relation the key crosses holds many rows for one row (a reverse FK or a many-to-many relation) — the filter joins it, and the query can return a row more than once. |
| `requires_extension` | The database extension the lookup needs (`"pg_trgm"` for the trigram lookups, `"unaccent"`), else `None`. |
| `dialects` | The names of the dialects the field and the lookup exist on (`frozenset({"postgresql"})` for an array field), `None` for every dialect — or for a field whose column type each dialect gives (`GeometryField`, `VectorField`), which `is_supported()` checks against the dialect asked about. |
| `is_supported(dialect)` | Whether a query on `dialect` runs the lookup — see below. |

### <a id="errors"></a>Errors

- A path naming no field, relation or annotation raises `FieldError`:
  `Unknown filter param 'tournament__nme': Tournament has no field 'nme'`.
- A lookup the field doesn't have raises `FieldError`: `Event.name has no lookup 'foo'`; so does a
  lookup outside the field's `supported_lookups` (an encrypted field).
- `pk` of a composite primary key takes only `pk=`, `pk__in=`, `pk__not=` and `pk__not_in=`; anything else is a
  `FieldError`.
- A lookup other than equality, membership or `isnull` on a forward relation to a composite key
  (`document__gt=`) raises `QueryError` — it would be ambiguous; filter each key column instead.
- A model not bound yet (see [Before the connections are set up](#before-the-connections-are-set-up))
  describes nothing: `get_lookup_info()`, `get_lookups()` and `get_ordering_info()`, on
  `Model._meta` and on a queryset alike, raise `ConfigurationError`:
  `Book is not bound yet: call Hare.bind_models() or Hare.init() before describing its filters or orderings`.

## <a id="dialect-support"></a>Dialect support

`dialect.filter_operators.supports_lookup(info)`, or `info.is_supported(dialect)`, tells whether a query on that
dialect runs the lookup. It is not supported when:

- the field doesn't exist on the dialect (`Field.SUPPORTED_DIALECTS`, e.g. an array or range field
  off PostgreSQL; a field with `COLUMN_TYPE_FROM_DIALECT` the dialect gives no column type), or a
  custom lookup was registered for other dialects only;
- the lookup's operator is one only dialects implement (`search`, the trigram lookups, the array,
  range and JSON container lookups) and this dialect doesn't implement it;
- the lookup needs an extension and the dialect has no extensions.

```python
from hare.dialects.dialect_registry import DialectRegistry

sqlite = DialectRegistry.get_dialect("sqlite")
info = Event._meta.get_lookup_info("name__search")
info.is_supported(sqlite)                                  # False
info.is_supported(DialectRegistry.get_dialect("postgresql"))  # True
```

A query running a lookup its connection's dialect doesn't support raises `UnSupportedError` before
its SQL is built: `Event.objects.filter(name__search=...) can't run on sqlite: the sqlite dialect doesn't
implement the __search lookup`.

## <a id="get_lookups"></a>`get_lookups()`

```python
lookups = Event._meta.get_lookups("modified", connection.dialect)
lookups[""]            # plain equality
lookups["year__gte"]   # LookupInfo(value_type=int, ...)
```

`path` is a field or relation, after any relations (`"tournament__name"`, `"tags"`, `"pk"`). The
result maps each lookup's suffix after `path` — `""` for plain equality, `"icontains"`,
`"year__gte"` — to its description, and holds only the lookups the dialect supports. A relation
itself has the lookups of its key (`""`, `in`, `not`, `not_in`, `isnull`, `not_isnull`; a forward
relation with a single key column also the comparisons of that column); `pk` of a composite primary
key has `""` and `in`.

A lookup can also need a feature of the connection the query runs on, beyond its dialect:
`posix_regex`/`iposix_regex` need `features.supports_posix_regex` — on SQLite a connection whose
DB_URL has `?install_regexp_functions=true`. `get_lookups()` describes the dialect and lists them;
a query using one on a connection without the feature raises `UnSupportedError` before it runs.

## <a id="get_ordering_info"></a>`get_ordering_info()`

```python
info = Event._meta.get_ordering_info("-tournament__name")
info.relations    # (Event.tournament,)
info.fields       # (Tournament.name,)
info.paths        # ("tournament__name",)
info.descending   # True
```

`OrderingInfo` has `name`, `model`, `relations`, `fields` (the fields ordered by), `paths` (the
names the query orders by), `transforms` (a path inside a JSON, array or range value, or a date
part: `get_ordering_info("created__year").transforms == ("year",)`), `descending`
and `crosses_to_many`. A forward FK/O2O orders by its own key column(s) with no join
(`"tournament"` → paths `("tournament_id",)`); the model's own `pk` by its key field(s) — every one
of a composite primary key, in key order (`("id", "version")`); a reverse or many-to-many relation
(`"tags"`) and a related `pk` (`"author__pk"`) by the related primary key field(s), read through the
relation as named. An unknown name raises `FieldError`:
`Unknown field nme for ordering: Event has no field 'nme'`.

## <a id="on-a-queryset"></a>On a queryset

`QuerySet.get_lookup_info()`, `get_lookups()` and `get_ordering_info()` also take the queryset's
annotations: a key may start with one, and it is described by the type of the annotation's value
(`Count()` is an integer).

```python
queryset = Tournament.objects.annotate(event_count=Count("events"))
queryset.get_lookup_info("event_count__gte").value_type    # int
```

## <a id="before-the-connections-are-set-up"></a>Before the connections are set up

Every description needs the models only, no connection. `Hare.bind_models()` binds the
models of a whole configuration — the relations between them, swappable models, each model's
filters and orderings — synchronously, and leaves no Hare context current:

```python
from hare import Hare

Hare.bind_models(config=CONFIG)  # the same config Hare.init() takes

Book._meta.get_lookup_info("author__name__icontains").value_type  # str
Book.objects.all().get_ordering_info("-published_at").descending           # True
```

It takes the configuration in every form `Hare.init()` does (a dict, a `HareConfig`, a file path or
`"module.VARIABLE"`) and describes every key a full `Hare.init()` describes. A framework
that builds its request handlers' signatures and schema when the application is created, before
its lifespan opens the connections, reads the filters from there. `Hare.init()` — or
`HareContext.init()` in any context, with a global fallback context as well — sets the
connections up later as usual.

`Model._meta.is_bound` tells whether a model is bound — `False` right after its class is
defined, `True` once any of these calls binds it:

```python
Book._meta.is_bound  # False
Hare.bind_models(config=CONFIG)
Book._meta.is_bound  # True
```

Until then every description raises `ConfigurationError` (see [Errors](#errors)). A queryset
made before binding (`BOOKS = Book.objects.all()` at module level) describes its keys once the model is
bound.

## <a id="validation-in-filter-and-order-by"></a>Validation in `.filter()` and `.order_by()`

`.filter()`, `.exclude()` and `.order_by()` check every key when called — the whole path through
relations, the field and the lookup, keys inside `Q` objects included:

```python
Event.objects.filter(tournament__nme="T")
# FieldError: Event.objects.filter(tournament__nme=...): Tournament has no field 'nme'
```

A queryset built before `Hare.init()` is checked when `init()` replays it, and `init()` reports every
invalid one with the place it was built.
