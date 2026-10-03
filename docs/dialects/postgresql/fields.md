# PostgreSQL fields

Everything on this page lives in `hare.dialects.postgresql`, the PostgreSQL dialect's own package,
and only works against a PostgreSQL connection. Declaring any of these field types on a model backed by a different dialect (SQLite)
raises `UnSupportedError` when its DDL is generated, rather than silently generating DDL that
doesn't make sense for that backend.

## `ArrayField` {: #arrayfield }

```python
ArrayField(base_field: Field, **kwargs)
```
A real Postgres array — the element type comes from an actual field instance, Django-style, not a
type-name string:

```python
class Article(Model):
    tags = fields.ArrayField(base_field=fields.TextField())
```

A `None` element is stored as a SQL `NULL` element whatever the `base_field` is (`[1, None, 3]` on an
`IntField` array); every other element goes through `base_field`'s own conversion and validation.

Native lookups: `contains`, `contained_by`, `overlap`, `len`, and paths to an element (`tags__0`),
a slice (`tags__0_2`) or the length (`tags__len`) — see
[Lookups and paths](#array-lookups).

### Lookups and paths {: #array-lookups }

Also a separate set: `exact`, `not`, `isnull`, `not_isnull`, `contains`, `contained_by`, `overlap`,
`len`, `item`.

```python
await Article.objects.filter(tags__contains=["python"])
await Article.objects.filter(tags__overlap=["python", "orm"])
```

`item` compares a single element by 0-indexed (Python-convention) position, translated internally
to Postgres's own 1-indexed array subscript:

```python
await Article.objects.filter(tags__item=(0, "python"))  # tags[1] = 'python'
```

On a nested array (`ArrayField(base_field=ArrayField(...))`) `len` counts the rows (the first
dimension), and `item` compares a whole row: `matrix__item=(0, [1, 2])`.

For annotating an element instead of filtering on it, use
[`ArrayItem`](functions.md) — same 0-indexed convention:

```python
await Article.objects.annotate(first_tag=ArrayItem("tags", 0)).values("first_tag")
```

A path through the array reads a value that takes its own type's lookups, as in Django:

| Segment | Reads | Type |
|---|---|---|
| `tags__0` | the element at a 0-indexed position (`-1` is the last one); `NULL` out of range | the element's |
| `tags__0_2` | the elements from 0 up to, not including, 2 | the array's |
| `tags__len` | the number of elements (rows of a nested array) - `0` for an empty array | integer |

Segments chain, through nested arrays too (`matrix__1__0`, `matrix__0__len`), and the same paths
work in `F()`, `values()`/`values_list()`, `order_by()`, `group_by()` and after a relation
(`articles__tags__0`):

```python
await Article.objects.filter(tags__0__startswith="py")
await Article.objects.filter(tags__len__gte=2)
await Article.objects.filter(tags__0_2__contains=["orm"])
await Article.objects.all().order_by("tags__0").values_list("tags__0", "tags__len")
```

## `CitextField` {: #citextfield }

```python
CitextField(**kwargs)
```
A Postgres `CITEXT` column — case-insensitive text: comparing, indexing, and sorting all work
without wrapping either side of a query in `UPPER()`/`LOWER()`. Requires the `citext` extension;
the migration autodetector adds a `CreateExtension("citext")` for it automatically wherever this
field is used — no `Meta.extensions` declaration needed (see
[Migration operations — extensions](../../migrations/operations.md#extensions)).

```python
class User(Model):
    email = CitextField()

await User.objects.filter(email="Someone@Example.com").first()  # matches "someone@example.com" too
```

`__contains`/`__startswith`/`__endswith` ignore case too - the column is compared as `citext`, not cast
to `VARCHAR`.

A value with a null byte raises `ValidationError`, the same as on `CharField`/`TextField`.

## `HStoreField` {: #hstorefield }

```python
HStoreField(**kwargs)
```
A Postgres `hstore` column - a flat map of string keys to string or `None` values, a `dict` in Python.
Requires the `hstore` extension; the migration autodetector adds a `CreateExtension("hstore")` for it
wherever this field is used. A key or value that isn't a string is written as its `str()`, as Django
does (`{"n": 5}` reads back as `{"n": "5"}`); a value that isn't a `dict`, or a null byte, raises
`ValidationError`.

```python
class Product(Model):
    attributes = HStoreField(null=True)

await Product.objects.filter(attributes__color="red")
await Product.objects.filter(attributes__has_keys=["color", "size"])
await Product.objects.all().values_list("attributes__color", "attributes__keys")
```

Lookups and transforms - see [Lookups and paths](#hstore-lookups).

### Lookups and paths {: #hstore-lookups }

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull` compare the whole value; `contains`/
`contained_by` take a `dict` of pairs (`@>`/`<@`); `has_key` takes a key, `has_keys`/`has_any_keys` a list
of keys (`?`/`?&`/`?|`).

Any other name after the field is a key: `attributes__color` is its value (text, `None` when the key
is missing) and takes the text lookups (`attributes__color__startswith="bl"`). `attributes__keys` and
`attributes__values` are every key/value as a text array, with the `ArrayField` lookups and paths
(`attributes__keys__contains=["size"]`, `attributes__keys__len=3`). A key named like a lookup is
reached through `contains`/`has_key`. The same paths work in `F()`, `values()`, `order_by()` and after a
relation.

```python
await Product.objects.filter(attributes__contains={"color": "red"})
await Product.objects.filter(attributes__size__in=["M", "L"])
await Product.objects.filter(attributes__keys__overlap=["size", "weight"])
```

## `TSVectorField` {: #tsvectorfield }

```python
TSVectorField(source_fields: Sequence[str] | str | None = None, config: str | None = None, weights: Sequence[str] | None = None, stored: bool = True, **kwargs)
```
When `stored=True` and `source_fields` are given, generates as `GENERATED ALWAYS AS (...) STORED` —
kept in sync by Postgres itself, no application code needed:

```python
class Article(Model):
    title = fields.CharField(max_length=300)
    body = fields.TextField()
    search_vector = TSVectorField(source_fields=("title", "body"), config="english")
```

## `PostGISField` {: #postgisfield }

```python
PostGISField()
```
Stores a `geography(Point,4326)` — WGS84 latitude/longitude with accurate DB-side geodesy. Requires
the `postgis` extension; the migration autodetector adds a `CreateExtension("postgis")` for it wherever
this field is used. The Python value is a **`(latitude, longitude)`** tuple.

```python
class Store(Model):
    location = PostGISField()

    class Meta:
        indexes = (GistIndex(fields=("location",)),)
```

Comes with a `within_km` lookup out of the box:

```python
await Store.objects.filter(location__within_km=(55.75, 37.62, 10))  # within 10 km of a point
```

For distance-based ordering/filtering beyond a simple radius, annotate with `STDistance`/`STDWithin`
(see [PostgreSQL functions](functions.md)).

## Range fields {: #range-fields }

**Range fields** — `RangeField` is the base (not for direct use); the Python value is a
`hare.dialects.postgresql.fields.ranges.Range`, or, as a shortcut, a plain `(lower, upper)` 2-tuple (a
half-open `[)` range):

```python
@dataclass(frozen=True)
class Range(Generic[T]):
    lower: T | None = None       # None = unbounded
    upper: T | None = None       # None = unbounded
    lower_inc: bool = True       # inclusive lower bound ("[")
    upper_inc: bool = False      # inclusive upper bound ("]") - Postgres's own [) default
    is_empty: bool = False
```

!!! warning "Empty vs. unbounded"
    Postgres canonicalizes any range whose bounds denote no values at all (e.g. equal bounds under
    the default half-open interpretation) to its own `'empty'` value — which decodes as
    `lower=None, upper=None`, **identical** to a genuinely unbounded range unless you also check
    `is_empty`. Round-tripping a decoded range without preserving `is_empty` silently turns "no
    values" into "every value".

An assigned range (on construction, `create()` or attribute assignment) is already stored in memory
the way Postgres returns it: bounds are coerced to the field's element type (a date string becomes
a `date`, a datetime bound follows `DatetimeField` - see below), a discrete range
(`IntRangeField`/`BigIntRangeField`/`DateRangeField`) is rewritten to `[)` (`Range(1, 5,
lower_inc=False, upper_inc=True)` becomes `Range(2, 6)`), an unbounded side is exclusive, and a
range with no values becomes the empty range. A lower bound greater than the upper one raises
`ValidationError`.

A `DateTimeRangeField` bound means what a `DatetimeField` value means: a naive bound is a wall clock
in the configured timezone under `use_tz=True` and in the system's local zone under `use_tz=False`,
and bounds read back in the configured timezone (`use_tz=True`) or as naive local wall-clock time
(`use_tz=False`) - so `during__contains=at` agrees with a `DatetimeField` holding the same naive
value.

An `infinity`/`-infinity` value (a `DATE`/`TIMESTAMPTZ` column, or a range bound) reads as the Python
extreme on both drivers — `date.max`/`date.min`, `datetime.max`/`datetime.min` — and writing that
extreme back stores `infinity`/`-infinity` again, not a finite date. A `DateTimeRangeField` bound
at infinity is UTC-aware under `use_tz=True` (`datetime.min.replace(tzinfo=UTC)`) and naive under
`use_tz=False`; a naive `datetime.max`/
`datetime.min` bound is taken as the same infinity, whatever the configured timezone.

| Field | Postgres type |
|---|---|
| `IntRangeField` | `int4range` |
| `BigIntRangeField` | `int8range` |
| `DecimalRangeField` | `numrange` |
| `DateRangeField` | `daterange` |
| `DateTimeRangeField` | `tstzrange` |

Lookups (`contains`, `overlap`, `fully_lt`, `adjacent_to`, ...) and paths to a bound
(`during__startswith`) or a flag (`during__isempty`) — see
[Lookups and paths](#range-lookups).

Range fields pair naturally with `ExclusionConstraint` for overlap-free scheduling — see
[Constraints](../../models/constraints-and-triggers.md#constraints).

### Lookups and paths {: #range-lookups }

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`, plus the Postgres range operators - each
takes a `Range` (or a `(lower, upper)` tuple), `contains` also a single value:

| Lookup | Operator | Matches rows where the range |
|---|---|---|
| `contains` | `@>` | contains the value or the whole range |
| `contained_by` | `<@` | lies within the range |
| `overlap` | `&&` | shares a value with the range |
| `fully_lt` | `<<` | lies entirely below it |
| `fully_gt` | `>>` | lies entirely above it |
| `not_lt` | `&>` | doesn't extend below it |
| `not_gt` | `&<` | doesn't extend above it |
| `adjacent_to` | <code>-&#124;-</code> | touches it without overlapping |

A path reads a part of the range, with that part's own lookups: `startswith`/`endswith` are the
lower/upper bound (of the element type - a `date` on a `DateRangeField`, `NULL` when unbounded or
empty), and `isempty`, `lower_inc`, `lower_inf`, `upper_inc`, `upper_inf` are booleans. They work in
`F()`, `values()`, `order_by()` and after a relation the same way:

```python
await Booking.objects.filter(during__fully_lt=Range(start, end))
await Booking.objects.filter(during__startswith__year=2024)
await Booking.objects.filter(during__upper_inf=True)
await Booking.objects.all().order_by("during__startswith").values("id", "during__endswith")
```
