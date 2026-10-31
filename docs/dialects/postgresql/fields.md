# PostgreSQL: fields

Everything on this page lives in `hare.dialects.postgresql`, the PostgreSQL dialect's own package, and
only works against a PostgreSQL connection. Declaring any of these field types on a model backed by a
different dialect (SQLite, ClickHouse) raises `UnSupportedError` when its DDL is generated, rather than
silently generating DDL that doesn't make sense for that database. Every field of the page but
`ArrayField` is imported from `hare.dialects.postgresql.fields`; `ArrayField` is
`hare.fields.ArrayField`, which runs on ClickHouse too — see
[ClickHouse types](../clickhouse/types.md#containers):

```python
from hare.dialects.postgresql.fields import (
    CitextField, FixedCharField, HStoreField, InetField, IntervalField, IntRangeField, LtreeField,
    NaiveDatetimeField, NaiveTimeField, NativeEnumField, PostGISField, Range, TSVectorField,
    # ... every field of this page
)
from hare.fields import ArrayField
```

## <a id="arrayfield"></a>`ArrayField`

```python
class ArrayField:
    def __init__(self, base_field: Field[Any], **kwargs: Any) -> None: ...
```
A real PostgreSQL array — the element type comes from an actual field instance, not a type-name string:

```python
class Book(Model):
    keywords = ArrayField(base_field=fields.TextField())
```

A `None` element is stored as a SQL `NULL` element whatever the `base_field` is (`[1, None, 3]` on an
`IntField` array); every other element goes through `base_field`'s own conversion and validation. Declare
`base_field` with `null=True` for an array that holds `None` elements: the
[pydantic schema](../../integrations/pydantic.md) types an element by it (`list[int]` or
`list[int | None]`), and ClickHouse stores a `None` element only in a `Nullable` one.

Native lookups: `contains`, `contained_by`, `overlap`, `len`, and paths to an element (`keywords__0`), a
slice (`keywords__0_2`) or the length (`keywords__len`) — see [Lookups and paths](#array-lookups).

### <a id="array-lookups"></a>Lookups and paths

Also a separate set: `exact`, `not`, `isnull`, `not_isnull`, `contains`, `contained_by`, `overlap`,
`len`, `item`.

```python
await Book.objects.filter(keywords__contains=["sea"])
await Book.objects.filter(keywords__overlap=["sea", "whaling"])
```

`item` compares a single element by 0-indexed (Python-convention) position, translated internally to
PostgreSQL's own 1-indexed array subscript:

```python
await Book.objects.filter(keywords__item=(0, "sea"))  # keywords[1] = 'sea'
```

On a nested array (`ArrayField(base_field=ArrayField(...))`) `len` counts the rows (the first dimension),
and `item` compares a whole row: `matrix__item=(0, [1, 2])`. A PostgreSQL array is rectangular:
sub-arrays of other lengths at one depth (`[[1, 2], [3]]`), or a value beside a sub-array
(`[[1, 2], None]`), raise `ValidationError` before anything is sent. An array of empty sub-arrays
(`[[]]`, `[[], []]`) is stored by PostgreSQL as an empty array and read back as `[]`; ClickHouse keeps
it.

For annotating an element instead of filtering on it, use [`ArrayItem`](functions.md) — same 0-indexed
convention:

```python
await Book.objects.annotate(first_keyword=ArrayItem("keywords", 0)).values("first_keyword")
```

A path through the array reads a value that takes its own type's lookups:

| Segment | Reads | Type |
|---|---|---|
| `keywords__0` | the element at a 0-indexed position (`-1` is the last one); `NULL` out of range | the element's |
| `keywords__0_2` | the elements from 0 up to, not including, 2 | the array's |
| `keywords__len` | the number of elements (rows of a nested array) — `0` for an empty array | integer |

Segments chain, through nested arrays too (`matrix__1__0`, `matrix__0__len`), and the same paths work in
`F()`, `values()`/`values_list()`, `order_by()`, `group_by()` and after a relation
(`books__keywords__0`):

```python
await Book.objects.filter(keywords__0__startswith="se")
await Book.objects.filter(keywords__len__gte=2)
await Book.objects.filter(keywords__0_2__contains=["whaling"])
await Book.objects.all().order_by("keywords__0").values_list("keywords__0", "keywords__len")
```

## <a id="wall-clock-fields"></a>`NaiveDatetimeField`, `NaiveTimeField`, `IntervalField`, `FixedCharField`

```python
class NaiveDatetimeField:
    def __init__(self, **kwargs: Any) -> None: ...


class NaiveTimeField:
    def __init__(self, **kwargs: Any) -> None: ...


class IntervalField:
    def __init__(self, **kwargs: Any) -> None: ...


class FixedCharField:
    def __init__(self, max_length: int, **kwargs: Any) -> None: ...
```

PostgreSQL's types hare's own fields don't write: `DatetimeField` and `TimeField` keep a moment
(`timestamptz`, `timetz`), `TimeDeltaField` a `bigint` of microseconds, `CharField` a `varchar`.

- **`NaiveDatetimeField`** — a `timestamp`: a date and a wall clock time without a time zone, held as a
  naive `datetime` whatever `use_timezone` says. It takes a naive datetime, a date (its midnight) or an
  ISO 8601 string; an aware datetime names a moment, not a wall clock, and raises `ValidationError`. It
  has the date parts of a `DatetimeField` (`__year`, `__hour`, ...), its `__date` and `__time`, and takes
  `Extract()`/`Trunc()` and `values("at__date")` — all read off the wall clock as it is, in no zone;
  `Trunc(..., "time")` gives a naive `time`. `F("at") ± timedelta` gives a wall clock and
  `F("at") - F("at2")` of two of them a `timedelta`; a wall clock and a moment (a `DatetimeField`, an
  aware `datetime`) don't mix in arithmetic, in `Coalesce()`, `Case()`, `Greatest()`, `Least()` and
  `NullIf()`, and `update()` doesn't assign one to the other (`update(at=F("moment"))`) — `FieldError`:
  they differ by a zone. A naive `datetime` literal beside a wall clock (`Coalesce(F("at"),
  datetime(2026, 7, 5, 9, 15))`) is that wall clock, in any configured zone; one inside another expression
  of literals alone (`Coalesce(F("at"), Case(When(..., then=datetime(...))))`) is a moment of the configured
  zone there, and refused beside the wall clock.
- **`NaiveTimeField`** — a `time`: a wall clock time of day, held as a naive `time`; a time with a
  `tzinfo` raises `ValidationError`. It has `__hour`, `__minute`, `__second`, and takes `Extract()` and
  `Trunc()` of the time of day.
- **`IntervalField`** — an `interval`, held as a `timedelta`. PostgreSQL keeps an interval's months apart
  from its days: one written by SQL (`'1 month'`) is read as 30 days a month and 365 days a year.
- **`FixedCharField(max_length)`** — a `character(max_length)`: PostgreSQL pads its text with spaces to
  `max_length`, so it is read back padded (`"ab"` comes back `"ab  "` from a `character(4)`) and compared
  ignoring the trailing spaces (`filter(code="ab")` finds it). It validates like `CharField`.

`inspectdb` maps `timestamp`, `time`, `interval` and `character(n)` columns onto these fields.

## <a id="citextfield"></a>`CitextField`

```python
class CitextField:
    def __init__(self, **kwargs: Any) -> None: ...
```
A PostgreSQL `CITEXT` column — case-insensitive text: comparing, indexing, and sorting all work without
wrapping either side of a query in `UPPER()`/`LOWER()`. Requires the `citext` extension; the migration
autodetector adds a `CreateExtension("citext")` for it automatically wherever this field is used — no
`Meta.extensions` declaration needed (see [Migration operations —
extensions](../../migrations/operations.md#extensions)).

```python
class Reader(Model):
    email = CitextField()

await Reader.objects.filter(email="Someone@Example.com").first()  # matches "someone@example.com" too
```

`__contains`/`__startswith`/`__endswith` ignore case too — the column is compared as `citext`, not cast
to `VARCHAR`.

A value with a null byte is refused by PostgreSQL itself, as on `CharField`/`TextField` and `JSONField` —
hare raises `ValidationError` for it (on a write, an update and a filter alike), not `OperationalError`.

## <a id="nativeenumfield"></a>`NativeEnumField`

```python
from hare.dialects.postgresql.fields import NativeEnumField

def NativeEnumField(
    enum_type: type[NativeEnumType],
    *,
    type_name: str | None = None,
    type_schema: str | None = None,
    **kwargs: Any,
) -> NativeEnumType: ...
```

A member of an enum in a column of a PostgreSQL `ENUM` type of its own —
`CREATE TYPE loan_status AS ENUM ('lent', 'overdue', 'returned')`. It reads and writes like
`CharEnumField` (a member or its value is accepted, a member comes back), and the database itself refuses
any other label and orders the column in the enum's order, not alphabetically.

```python
class LoanStatus(Enum):
    LENT = "lent"
    OVERDUE = "overdue"
    RETURNED = "returned"


class Loan(Model):
    status = NativeEnumField(LoanStatus)
    previous_status = NativeEnumField(LoanStatus, null=True)  # the same type


await Loan.objects.filter(status__in=[LoanStatus.LENT, "overdue"]).order_by("status")  # lent, overdue, returned
```

- **The type.** Its name is `type_name`, else the enum's class name in snake case (`LoanStatus` ->
  `loan_status`) — a lowercase identifier of at most 63 characters. Its labels are the members' values as
  text (`str(member.value)`, so an `IntEnum` works too), in the enum's order, each at most 63 bytes.
  The type lives in `type_schema`, else in the connection's default schema whatever the model's
  `Meta.schema` — so two schemas may each hold a type of one name with labels of their own, each named by
  its `type_schema`. Fields naming one type in one schema share it; two of them with different labels
  raise `ConfigurationError`. An enum without members, or a wrong name or schema, raises
  `ConfigurationError` when the field is declared.
- **The schema.** `generate_schemas()` creates the types before the tables (`safe=True` keeps an existing
  one). The migration autodetector writes `CreateEnumType` before the first column of a type,
  `AlterEnumType` when the enum's members change, and `DropEnumType` after its last column is gone — see
  [Migration operations — ENUM types](../../migrations/operations.md#enum-types). The schema a
  `type_schema` names is created as a model's `Meta.schema` is (`CreateSchema`). Turning a
  `CharEnumField` into a `NativeEnumField`, or moving the column to another type (another `type_name` or
  `type_schema`), converts it in place through its text (`ALTER COLUMN ... TYPE ... USING`).
- **Changing the members.** New members added while the old ones keep their order become new labels in
  place (`ALTER TYPE ... ADD VALUE`) — a value added this way can't be used in the transaction that adds
  it, so write such rows in a later migration. A removed or reordered member replaces the type: the
  columns of it (array columns too) and their defaults are converted to the new type in one statement, so
  no row may still hold a removed label — update those rows first: a migration finding one raises
  `FieldNarrowingDataLossError` naming the table and the column before it changes anything.
- **Filters** take the generic set — equality, `__in`, `__isnull` and the comparisons, in the enum's
  order. A label outside the enum raises `ValidationError` before the query runs.
- PostgreSQL only (`features.supports_enum_types`); on another database the field raises
  `UnSupportedError`.

## <a id="network-fields"></a>Network fields: `InetField`, `CidrField`, `MacAddressField`

```python
from hare.dialects.postgresql.fields import CidrField, InetField, MacAddressField


class Host(Model):
    address = InetField()          # inet
    subnet = CidrField(null=True)  # cidr
    mac = MacAddressField(null=True, unique=True)  # macaddr
```

- **`InetField`** — an IPv4 or IPv6 host address, with the prefix length of its network when it has one.
  It is read as an `ipaddress.IPv4Address`/`IPv6Address`, or as an `IPv4Interface`/`IPv6Interface`
  (address and network) when the prefix is shorter than the full length (`10.0.1.7/16`). Text and
  `ipaddress` objects (an address, an interface, a network) are taken.
- **`CidrField`** — an IPv4 or IPv6 network, read as an `ipaddress.IPv4Network`/`IPv6Network`; text and
  `ipaddress` objects are taken, an address as the network of it alone (`/32`, `/128`). A network with
  host bits set (`10.0.0.1/8`) raises `ValidationError`, as the column refuses it.
- **`MacAddressField`** — a 6-byte MAC address in any usual form (`08:00:2B:01:02:03`,
  `08-00-2b-01-02-03`, `0800.2b01.0203`, `08002b010203`), read and written as `08:00:2b:01:02:03`.
- A value that isn't of the field's type raises `ValidationError` before the query runs — in a write and
  in a filter alike.

The three filter by equality, `__not`, `__in`/`__not_in`, `__isnull` and the comparisons (`__gt`, ...,
`__range`, in the database's order); they have no text lookups. `InetField` and `CidrField` also filter
by subnet — the value is an address or a network, as text or an `ipaddress` object, or `F()` of another
column:

| Lookup | Operator | True when the column's address or network |
|---|---|---|
| `__net_contained` | `<<` | lies inside the value, not equal to it |
| `__net_contained_or_equal` | `<<=` | lies inside the value or equals it |
| `__net_contains` | `>>` | holds the value, not equal to it |
| `__net_contains_or_equal` | `>>=` | holds the value or equals it |
| `__net_overlaps` | `&&` | holds the value or lies inside it |

```python
await Host.objects.filter(address__net_contained="10.0.0.0/8")
await Host.objects.filter(subnet__net_contains=request_ip)
await Host.objects.filter(address__net_contained_or_equal=F("subnet"))
```

`__family` (4 or 6) and `__masklen` (the prefix length) read a part of the value as an int, in filters,
`values()`, `F()` and `order_by()`: `filter(address__family=6)`, `values_list("subnet__masklen")`.
PostgreSQL only: on another database the fields raise `UnSupportedError`.

## <a id="hstorefield"></a>`HStoreField`

```python
class HStoreField:
    def __init__(self, **kwargs: Any) -> None: ...
```
A PostgreSQL `hstore` column — a flat map of string keys to string or `None` values, a `dict` in Python.
Requires the `hstore` extension; the migration autodetector adds a `CreateExtension("hstore")` for it
wherever this field is used. A key or value that isn't a string is written as its `str()` (`{"n": 5}`
reads back as `{"n": "5"}`); a value that isn't a `dict`, or a null byte, raises `ValidationError`.

```python
class Edition(Model):
    attributes = HStoreField(null=True)

await Edition.objects.filter(attributes__binding="hardcover")
await Edition.objects.filter(attributes__has_keys=["binding", "format"])
await Edition.objects.all().values_list("attributes__binding", "attributes__keys")
```

Lookups and transforms — see [Lookups and paths](#hstore-lookups).

### <a id="hstore-lookups"></a>Lookups and paths

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull` compare the whole value; `contains`/
`contained_by` take a `dict` of pairs (`@>`/`<@`); `has_key` takes a key, `has_keys`/`has_any_keys` a
list of keys (`?`/`?&`/`?|`).

Any other name after the field is a key: `attributes__binding` is its value (text, `None` when the key is
missing) and takes the text lookups (`attributes__binding__startswith="hard"`). `attributes__keys` and
`attributes__values` are every key/value as a text array, with the `ArrayField` lookups and paths
(`attributes__keys__contains=["format"]`, `attributes__keys__len=3`). A key named like any lookup (`in`,
`contains`, `gt`, ...) is the lookup, never the key — `attributes__in=[...]` compares the whole value and
`attributes__gt=...` raises `FieldError` as on any field without `__gt`; such a key is reached through
`contains`/`has_key`. The same paths work in
`F()`, `values()`, `order_by()` and after a relation.

```python
await Edition.objects.filter(attributes__contains={"binding": "hardcover"})
await Edition.objects.filter(attributes__format__in=["A5", "B5"])
await Edition.objects.filter(attributes__keys__overlap=["format", "weight"])
```

## <a id="tsvectorfield"></a>`TSVectorField`

```python
class TSVectorField:
    def __init__(
        self,
        source_fields: Sequence[str] | str | None = None,
        config: str | None = None,
        weights: Sequence[str] | None = None,
        stored: bool = True,
        **kwargs: Any,
    ) -> None: ...
```
When `stored=True` and `source_fields` are given, generates as `GENERATED ALWAYS AS (...) STORED` — kept
in sync by PostgreSQL itself, no application code needed:

```python
class Book(Model):
    title = fields.CharField(max_length=300)
    description = fields.TextField()
    search_vector = TSVectorField(source_fields=("title", "description"), config="english")
```

A migration changing the type of a source field's column drops the vector's column before the change and
adds it after — PostgreSQL refuses to change a column a generated column reads; the vectors are computed
again.

## <a id="ltreefield"></a>`LtreeField`

An `ltree` column — a node's place in a tree as the path of labels from the root
(`Top.Science.Astronomy`), a `str` in Python. One column answers "every descendant of", "every ancestor
of" and pattern questions with one indexed condition, without a recursive query. Needs the `ltree`
extension; the migration autodetector adds `CreateExtension("ltree")` wherever the field is used.

```python
from hare.dialects.postgresql.fields import LtreeField
from hare.dialects.postgresql.indexes import GistIndex


class Category(Model):
    path = LtreeField(unique=True)

    class Meta:
        indexes = (GistIndex(fields=("path",)),)  # serves the tree and pattern lookups
```

A path is written as text or as a list or tuple of labels (`["Top", "Science"]` is `"Top.Science"`, `[]`
and `""` the empty path) and read as text. A label holds letters, digits, `_` and `-` (the hyphen needs
PostgreSQL 16) and is not empty; any other path — `"Top..Science"`, a label with a space or a non-Latin
letter, a value that isn't a `str` or a list of `str` — raises `ValidationError` before the query runs.

Equality, `__not`, `__in`/`__not_in`, `__isnull` and the comparisons (`__gt`, ..., `__range` — the tree's
depth-first order, a parent before its children) work as for any field; there are no text lookups. The
tree lookups take a path (text, a list of labels, or `F()` of another column):

| Lookup | Operator | True when the column's path |
|---|---|---|
| `__ancestor_of` | `@>` | is the value or one of its ancestors |
| `__descendant_of` | `<@` | is the value or one of its descendants |
| `__matches` | `~` | matches the `lquery` pattern (a `str`) |
| `__matches_any` | `?` | matches one of the `lquery` patterns (a non-empty list of `str`) |
| `__matches_text` | `@` | matches the `ltxtquery` query (a `str`) |

```python
await Category.objects.filter(path__descendant_of="Top.Science")       # the subtree, root included
await Category.objects.filter(path__ancestor_of=category.path)        # the breadcrumbs
await Category.objects.filter(path__matches="*.Astronomy.*")          # any node under an Astronomy
await Category.objects.filter(path__matches="Top.*{1}")               # the children of Top
await Category.objects.filter(path__matches_text="Astro*% & !pictures@")
```

An `lquery` pattern matches labels one by one: `*` is any number of labels, `*{1}` exactly one, `Astro*`
a label starting with `Astro`, `a|b` either label, `!a` any label but `a`; an `ltxtquery` combines words
with `&`, `|` and `!`, where `*` is a prefix, `%` a word inside a label separated by `_`, and `@` ignores
case — the full syntax is in the [ltree
documentation](https://www.postgresql.org/docs/current/ltree.html).

`path__depth` is the number of labels (an int) in filters, `values()`, `F()` and `order_by()`:
`filter(path__depth=2)`. `Subpath(field, offset, length=None)` reads a part of the path as a path — the
labels from the 0-based `offset` (negative from the end), `length` of them or up to the end. A position
past the path is PostgreSQL's own `subpath()`: a positive one raises `OperationalError`; a negative one
past the start counts from the end once more (`-4` on a path of three labels reads as `-1`), and one past
twice the path's length raises `OperationalError`:

```python
from hare.dialects.postgresql.functions import Subpath

await Category.objects.annotate(section=Subpath("path", 1, 1)).values_list("section", flat=True)
```

PostgreSQL only: on another database the field raises `UnSupportedError`.

## <a id="postgisfield"></a>`PostGISField`

```python
class PostGISField:
    def __init__(self, **kwargs: Any) -> None: ...
```
Stores a `geography(Point,4326)` — WGS84 latitude/longitude with accurate geodesy computed by the
database. Requires the `postgis` extension; the migration autodetector adds a
`CreateExtension("postgis")` for it wherever this field is used. The Python value is a
**`(latitude, longitude)`** tuple. For other geometry types, SRIDs, planar columns and the spatial
lookups and functions, see [Geographic data (GIS)](../search-and-geodata/gis.md).

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

The radius is a finite number of zero or more (`int`, `float` or `Decimal`); a point is checked as a written
value is. Anything else raises `ValidationError` before the query is sent.

For distance-based ordering/filtering beyond a simple radius, annotate with `STDistance`/`STDWithin` (see
[PostgreSQL functions](functions.md)).

## <a id="range-fields"></a>Range fields

**Range fields** — `RangeField` is the base (not for direct use); the Python value is a
`hare.dialects.postgresql.fields.Range`, or, as a shortcut, a plain `(lower, upper)` 2-tuple (a half-open
`[)` range), or a mapping of its attributes (`{"lower": 1, "upper": 10}`) — the form a model's pydantic
schema writes, whose bounds it checks as the column's type, so `create(**schema.model_dump())` takes it:

```python
@dataclass(frozen=True)
class Range(Generic[T]):
    lower: T | None = None       # None = unbounded
    upper: T | None = None       # None = unbounded
    lower_inc: bool = True       # inclusive lower bound ("[")
    upper_inc: bool = False      # inclusive upper bound ("]") - Postgres's own [) default
    is_empty: bool = False
```

> [!WARNING]
> **Empty vs. unbounded**
>
> PostgreSQL canonicalizes any range whose bounds denote no values at all (e.g. equal bounds under the
> default half-open interpretation) to its own `'empty'` value — which decodes as
> `lower=None, upper=None`, **identical** to a genuinely unbounded range unless you also check
> `is_empty`. Round-tripping a decoded range without preserving `is_empty` silently turns "no values"
> into "every value".

An assigned range (on construction, `create()` or attribute assignment) is already stored in memory the
way PostgreSQL returns it: bounds are coerced to the field's element type (a date string becomes a
`date`, a datetime bound follows `DatetimeField` — see below), a discrete range
(`IntRangeField`/`BigIntRangeField`/`DateRangeField`) is rewritten to `[)`
(`Range(1, 5, lower_inc=False, upper_inc=True)` becomes `Range(2, 6)`), an unbounded side is exclusive,
and a range with no values becomes the empty range. A lower bound greater than the upper one raises
`ValidationError`, and so does a bound of an integer range outside its type or with a fraction
(`Range(1.9, 3)`, `span__contains=1.5`) or a `bool` one, as an `IntField` refuses it, and a NaN bound of
a `DecimalRangeField`, which orders against no other.

A `Range` as a field's `default` — and a list of them for a multirange — is written into a migration
as the call building it (`default=Range(1, 5)`).

A migration changing a range field to a range of another type (`IntRangeField` to `BigIntRangeField` or
`DecimalRangeField`, the same for a multirange or an array of ranges) converts the column through each
value's text (`USING span::text::int8range`): PostgreSQL casts no range type to another. A bound the new
type can't hold stops the migration with PostgreSQL's error.

A `DateTimeRangeField` bound means what a `DatetimeField` value means: a naive bound is a wall clock in
the configured timezone under `use_timezone=True` and in the system's local zone under
`use_timezone=False`, and bounds read back in the configured timezone (`use_timezone=True`) or as naive
local wall-clock time (`use_timezone=False`) — so `during__contains=at` agrees with a `DatetimeField`
holding the same naive value.

An `infinity`/`-infinity` value (a `DATE`/`TIMESTAMPTZ` column, or a range bound) reads as the Python
extreme on both drivers — `date.max`/`date.min`, `datetime.max`/`datetime.min` — and writing that extreme
back stores the finite day or moment it is (see [Extreme dates](../../models/field-types.md#extreme-dates)).
A `DateTimeRangeField` bound read at infinity is UTC-aware under `use_timezone=True`
(`datetime.min.replace(tzinfo=UTC)`) and naive under `use_timezone=False`; a naive `datetime.max`/
`datetime.min` bound is taken as that UTC moment, whatever the configured timezone.

| Field | PostgreSQL type |
|---|---|
| `IntRangeField` | `int4range` |
| `BigIntRangeField` | `int8range` |
| `DecimalRangeField` | `numrange` |
| `DateRangeField` | `daterange` |
| `DateTimeRangeField` | `tstzrange` |

Lookups (`contains`, `overlap`, `fully_lt`, `adjacent_to`, ...) and paths to a bound
(`during__startswith`) or a flag (`during__isempty`) — see [Lookups and paths](#range-lookups).

Range fields pair naturally with `ExclusionConstraint` for overlap-free scheduling — see
[Constraints](../../models/constraints-and-triggers.md#constraints).

### <a id="range-lookups"></a>Lookups and paths

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`, plus the PostgreSQL range operators — each takes
a `Range` (or a `(lower, upper)` tuple), `contains` also a single value:

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
lower/upper bound (of the element type — a `date` on a `DateRangeField`, `NULL` when unbounded or empty),
and `isempty`, `lower_inc`, `lower_inf`, `upper_inc`, `upper_inf` are booleans. They work in `F()`,
`values()`, `order_by()` and after a relation the same way:

```python
await Booking.objects.filter(during__fully_lt=Range(start, end))
await Booking.objects.filter(during__startswith__year=2024)
await Booking.objects.filter(during__upper_inf=True)
await Booking.objects.all().order_by("during__startswith").values("id", "during__endswith")
```

## <a id="multirange-fields"></a>Multirange fields

A multirange holds several non-overlapping ranges in one column — a room's busy days, the opening hours
of a library, the free seats of a row:

```python
from hare.dialects.postgresql.fields import DateMultiRangeField, IntMultiRangeField


class RoomSchedule(Model):
    busy_days = DateMultiRangeField(null=True)  # datemultirange
    free_seats = IntMultiRangeField(default=list)  # int4multirange
```

| Field | PostgreSQL type | Each range as |
|---|---|---|
| `IntMultiRangeField` | `int4multirange` | `IntRangeField` |
| `BigIntMultiRangeField` | `int8multirange` | `BigIntRangeField` |
| `DecimalMultiRangeField` | `nummultirange` | `DecimalRangeField` |
| `DateMultiRangeField` | `datemultirange` | `DateRangeField` |
| `DateTimeMultiRangeField` | `tstzmultirange` | `DateTimeRangeField` |

Each is a `MultiRangeField` setting its `SQL_TYPE` and the `RANGE_FIELD_CLASS` of its ranges — a
multirange of a range field of your own subclasses it the same way.

The Python value is a list of `Range` — each also as a `(lower, upper)` tuple or a mapping of its
attributes, as a model's schema writes one — and `[]` is the empty multirange. Every range is taken the
way its range field takes it (bounds coerced, a discrete range made `[)`, a `DateTimeMultiRangeField`
bound read in the configured timezone), and the list is held the way PostgreSQL stores it: empty ranges
dropped, the rest sorted by their lower bound, and overlapping or touching ranges merged into one — in
memory as soon as a value is assigned, not only after a read:

```python
schedule = RoomSchedule(free_seats=[(8, 9), (1, 3), (3, 5), Range(4, 4)])
schedule.free_seats  # [Range(1, 5), Range(8, 9)]
```

Two ranges touching at a bound merge when either side includes that bound: `[1, 2)` and `[2, 3)` become
`[1, 3)`, while `[1, 2)` and `(2, 3)` stay apart (2 belongs to neither). A value that isn't a list of
ranges — a single `Range` or `(lower, upper)` tuple among them — raises `ValidationError` naming the
field and the position of the bad range (`busy_days[1]: ...`), as does a range its range field refuses.

### <a id="multirange-lookups"></a>Lookups and paths

The lookups of a range field ([Lookups and paths](#range-lookups)) — `exact`, `not`, `isnull`,
`not_isnull`, `contains`, `contained_by`, `overlap`, `fully_lt`, `fully_gt`, `not_lt`, `not_gt`,
`adjacent_to`. The value is a list of ranges, or one `Range`/`(lower, upper)` tuple standing for a
multirange of that range alone; `contains` also takes one value of the element type:

```python
await RoomSchedule.objects.filter(busy_days__contains=date(2024, 1, 11))
await RoomSchedule.objects.filter(busy_days__overlap=[(start, end), (other_start, other_end)])
await RoomSchedule.objects.filter(free_seats__contains=(5, 7))
```

The paths of a range field read the multirange as a whole — `startswith`/`endswith` are the lower bound
of its first range and the upper bound of its last, `isempty`, `lower_inc`, `lower_inf`, `upper_inc`,
`upper_inf` its flags — and `span` is the smallest range holding every member (a `Range`, `range_merge()`
in SQL): `values_list("busy_days__span")`, `filter(busy_days__startswith__gte=day)`.

`RangeAgg(field)` (PostgreSQL's `RANGE_AGG`) merges a range column of each group into a multirange, read
through the multirange field of that range field — see [Functions](functions.md):

```python
await Booking.objects.values("room").annotate(busy=RangeAgg("during"))
# [{"room": 1, "busy": [Range(date(2024, 1, 1), date(2024, 1, 6)), ...]}, ...]
```

Multiranges need PostgreSQL 14, the oldest version hare supports; on another database the fields raise
`UnSupportedError`.

## <a id="postgresql-validators"></a>Validators

`hare.dialects.postgresql.validators`, for `validators=[...]` of a field:

| Validator | Checks |
|---|---|
| `KeysValidator(keys, strict=False, message=None)` | an `HStoreField` mapping has every key of `keys` — and no other with `strict=True` |
| `RangeMinValueValidator(limit_value, message=None)` | the range's lower bound is at least `limit_value` (an unbounded lower side fails) |
| `RangeMaxValueValidator(limit_value, message=None)` | the range's upper bound is at most `limit_value` (an unbounded upper side fails) |

An empty range passes both range validators. The element count of an `ArrayField` is checked with
`MaxLengthValidator`/`MinLengthValidator`, which take lists.

```python
class Edition(Model):
    attributes = HStoreField(validators=[KeysValidator(["binding", "format"])])
    reader_ages = IntRangeField(validators=[RangeMinValueValidator(0), RangeMaxValueValidator(100)])
```
