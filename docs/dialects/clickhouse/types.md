# ClickHouse: types

Every field of hare has a ClickHouse column type; `hare.dialects.clickhouse.fields` adds the types only
ClickHouse has, and the containers of `hare.fields` hold any field, each other too, to any depth.
`hare inspectdb` writes every one of them back as its field, and `hare drift` compares them as whole
trees.

## <a id="fields"></a>Fields and their columns

| Field | Column |
|---|---|
| `SmallIntField`, `IntField`, `BigIntField` | `Int16`, `Int32`, `Int64` |
| `Int8Field`, `UInt8Field`, `UInt16Field`, `UInt32Field`, `UInt64Field`, `UInt128Field`, `UInt256Field`, `Int128Field`, `Int256Field` | `Int8`, `UInt8` ... `Int256` — `hare.dialects.clickhouse.fields`, each a `ClickhouseIntegerField` taking only the values its type holds; `Int8Field` is the sign column of a `CollapsingMergeTree` |
| `FloatField`, `Float32Field` | `Float64`, `Float32` |
| `DecimalField(max_digits, decimal_places)` | `Decimal(P, S)` |
| `BooleanField` | `Bool` |
| `CharField`, `TextField` | `String` |
| `FixedStringField(length)` | `FixedString(length)` |
| `DatetimeField` | `DateTime64(6, 'UTC')` — microseconds, stored in UTC |
| `DateField` | `Date32` |
| `ClickhouseDatetimeField(precision=None, timezone="UTC")` | `DateTime('UTC')` — whole seconds; `DateTime64(precision, 'UTC')` with a `precision` |
| `ClickhouseDateField` | `Date` |
| `TimeField` | `String` — the ISO time of day, ordered and compared as PostgreSQL's `TIMETZ` (by the UTC time, then the offset) |
| `TimeDeltaField` | `Int64` — microseconds |
| `UUIDField` | `UUID` — ordered and compared by its text, as PostgreSQL orders the 16 bytes (ClickHouse's own order takes the lower 64 bits first); such an `ORDER BY` or `__gt` can't use the table's sorting key |
| `BinaryField` | `String` of the bytes' lowercase hex |
| `JSONField` | `JSON` from ClickHouse 25.3, `String` before — see [JSON](differences.md#json) |
| `CharEnumField`, `IntEnumField` | `Enum8`/`Enum16` — see [Enums](#enums) |
| `IPAddressField`, `IPv4AddressField` | `IPv6` (an IPv4 address mapped into it, read back as IPv4; ordered and compared as PostgreSQL's `inet`, IPv4 first), `IPv4` |
| `ArrayField`, `MapField`, `TupleField`, `NestedField` | `Array`, `Map`, `Tuple`, `Array(Tuple(...))` — see [Containers](#containers) |
| `LowCardinalityField(field)` | `LowCardinality(T)` |
| `VariantField([...])`, `DynamicField()` | `Variant(...)`, `Dynamic` |
| `GeometryField` | `Point`, `LineString`, `Polygon`, `MultiLineString`, `MultiPolygon` — see [Geo](#geo) |

`VariantField` and `DynamicField` need ClickHouse 25.3, where the types are no longer experimental, and
the line geometries a server newer than 24.3: the connection reads which of these types its server has,
and a table or a column of a type it lacks raises `UnSupportedError` before its DDL.

A nullable field's column is `Nullable(...)` — a leaf's alone: an array, a map and a tuple are never
`Nullable` (see [Containers](#containers)). An integer column wraps around on overflow, so hare checks
the ranges of integer and decimal values itself before writing; an unsigned 64-bit value above 2⁶³ and
the 128- and 256-bit integers are Python `int`. ClickHouse's integer arithmetic wraps around too: an
`update()` or `save()` setting a field to a sum, difference or product of integers (`F("total") + 1`)
computes it as an `Int256`, so a result out of the field's range raises `ValidationError` and no row is
written. In a query that reads the result (`annotate(next=F("total") + 1)`) a 64-bit sum is computed as
the server does and wraps around past the type's range, where PostgreSQL raises `bigint out of range` —
the wide arithmetic would cost reads several times the time. A remainder of integers (`F("count") % -2`)
takes the dividend's sign, as PostgreSQL's: ClickHouse's own `modulo()` of a column by a negative divisor
gives the other sign.

ClickHouse reads a number literal as the narrowest type holding it, and an unsigned one shares no type
with a negative one. So hare writes an integer beyond `Int64` through a wide type
(`toInt128('18446744073709551615')`), the values of the rows of a `bulk_update()` and of a `VALUES` list
as their column's type, and a literal branch of a `Case` (or `Coalesce`) of an integer result as that
type (`accurateCast`, refusing a value it doesn't hold). A UUID literal is read as a `UUID` (`toUUID`),
and a dict branch of a `Case` of a map result as that map — a `Case` mixing literals and a column of the
type works as on PostgreSQL.

- **`Float32Field`** reads a value back as the shortest decimal that is the same float32 — `0.1`, not the
  `0.10000000149011612` the drivers give; a value beyond its range, or so close to zero it would be
  stored as 0 (`1e-50`), raises `ValidationError`, as PostgreSQL's `real` refuses it.
- **`FixedStringField(length)`** holds a text of at most `length` bytes in UTF-8, padded with zero bytes
  and read back without them; a text holding a zero byte of its own raises `ValidationError`, as the
  server would read it as the text's end.
- **`ClickhouseDatetimeField`** keeps a moment in ClickHouse's own `DateTime` (whole seconds, 1970
  to 2106) or, with `precision` (0 to 9 digits after the second), `DateTime64(precision)` (1900 to 2299),
  in the column's `timezone` — an IANA name, `None` for the server's own; it reads and writes like
  `DatetimeField`, whose lookups (`__year`, `__hour`, ...) it has. A written moment is rounded to the
  column's precision, a tie away from zero, as PostgreSQL rounds a `timestamptz(p)`; one outside a
  `DateTime` column's range raises `ValidationError`. **`ClickhouseDateField`** keeps a day in a `Date`
  (1970-01-01 to 2149-06-06), a day outside it raising `ValidationError`. Both are in
  `hare.dialects.clickhouse.fields`; `inspectdb` maps these columns onto them.

## <a id="enums"></a>Enums

A `CharEnumField` or an `IntEnumField` is an `Enum8`/`Enum16` of labels and numbers. ClickHouse keeps the
numbers and reads the labels off the type, so a label keeps its number for good:

- an `IntEnumField`'s labels are its members' names, numbered by their values;
- a `CharEnumField`'s labels are its stored values, numbered from a checksum of the label — a member
  added, removed or moved changes no other label's number. The server would compare and sort the column
  by those numbers, so hare reads it as its label there (`toString(status)`): `order_by()`, `__gt` and
  `Min`/`Max` follow the text, as on PostgreSQL.

A migration changes the members with `MODIFY COLUMN`. Removing a member checks first that no row holds it
— the server refuses to drop a label in use.

An `Enum16` column takes no arithmetic, average or math function, so an `IntEnumField` is read by its
number there (`toInt16(level)`): `F("level") + 1`, `Avg("level")` and `Abs("level")` give what PostgreSQL
gives. `Max`/`Min` keep the member.

## <a id="containers"></a>Containers

```python
from hare import fields


class Delivery(Model):
    tags = fields.ArrayField(fields.CharField(max_length=20))
    prices = fields.MapField(fields.CharField(max_length=3), fields.DecimalField(max_digits=10, decimal_places=2))
    point = fields.TupleField({"lon": fields.FloatField(), "lat": fields.FloatField()})
    items = fields.NestedField({"isbn": fields.CharField(max_length=20), "quantity": fields.IntField()})
    history = fields.ArrayField(
        fields.MapField(fields.CharField(max_length=10), fields.ArrayField(fields.IntField(null=True)))
    )
```

| Field | Value | Paths | Lookups |
|---|---|---|---|
| `ArrayField(field)` | a list | `tags__0` an element (from 0, negative from the end), `tags__0_2` a slice, `tags__len` | `__contains`, `__contained_by`, `__overlap`, `__len`, `__item=(index, value)` |
| `MapField(key_field, value_field)` | a dict | `prices__eur` the value of a key (the value type's default for a missing key; a segment named like a lookup — `prices__in` — is that lookup, which a map refuses as an array does), `prices__keys`, `prices__values`, `prices__len` | `__has_key`, `__has_keys`, `__has_any_keys`, `__len` |
| `TupleField([...])` / `TupleField({...})` | a tuple / a dict by name | `point__0`, `point__lat` | those of the element |
| `NestedField({...})` | a list of dicts | `items__0` a row, `items__isbn` the column of every row, `items__len` | the array lookups |

Each value inside a container is written and read by the field holding it — a decimal, a moment, a UUID,
an enum at any depth — and a path of any length reads through them: `history__0__open__1__gt=3` is the
second number under the key `open` of the first map. A lookup at the end of a path is the one of the
field there.

- **`NULL`.** An array, a map and a tuple are never `Nullable` in ClickHouse: `null=True` on a container
  writes `None` as an empty value, read back empty. Without it, a container left unset or set to `None`
  raises `ValidationError` on every write, as PostgreSQL refuses `NULL` there — the server would store an
  empty array or a tuple of zeros instead. `null=True` on a field inside makes that element `Nullable`.
  So `__isnull=True` on a container matches no row — a `None` written is read back empty, which `__len=0`
  finds — and `Min`/`Max` of an array, a map or a tuple over no rows give its empty value (an empty array
  or map, a tuple of zeros), not `None`.
- **Order.** An array and a tuple order and compare element by element, each UUID, time of day, address
  and `CharEnumField` value inside as its own column does — as PostgreSQL orders them — in `order_by()`,
  `Min`/`Max` and a filter through a path (`statuses__0__gt`). A map, which PostgreSQL has no type of,
  orders as ClickHouse orders it.
- **Map keys** are integers, texts, `FixedString`, UUIDs, dates, moments and enums (also `LowCardinality`
  of text or UUIDs) holding no `NULL` — another key field raises `ConfigurationError` when the field is
  declared. A path names a key by its value as the key field reads it: `counts__online` is the value
  under the enum member `"online"`.
- **Errors** name the path to the value they are about: `tags[2]`, `prices['eur']`, `point.1`.
- **A migration** compares the whole tree of the field and changes any level with `MODIFY COLUMN`; a
  narrowing anywhere in it checks the data first.

`ArrayField` runs on PostgreSQL too — a nested `ArrayField(ArrayField(...))` is a multidimensional array
there; `MapField`, `TupleField` and `NestedField` are ClickHouse's alone.

## <a id="low-cardinality"></a>`LowCardinalityField`

`LowCardinalityField(fields.CharField(max_length=2))` stores the values as a dictionary of the distinct
ones — for a column with few of them (a country, a status). Its values, lookups and paths are those of
the field it wraps; `null=True` on it is `LowCardinality(Nullable(T))`. It wraps text, a
`FixedStringField` or a `UUIDField`: ClickHouse keeps no `LowCardinality` of an enum, a decimal or a
moment, and one of a number, a date or an address only under a server setting, so another field raises
`ConfigurationError` when its column type is built. It wraps no container.

## <a id="variant-and-dynamic"></a>`VariantField` and `DynamicField`

```python
from hare.dialects.clickhouse.fields import DynamicField, VariantField

reading = VariantField([fields.IntField(), fields.CharField(max_length=20)], null=True)
value = DynamicField(null=True)
```

A `VariantField` holds a value of one of its fields' types. A value is taken by the first field whose
type it is exactly, else by the first it is an instance of, else by the first converting it; a value read
is given back by the field of its ClickHouse type — so give fields read as distinct Python types. A
filter matches the values of the filter value's field only, and `reading__0__gt=3` reads the values of
the field at that position, `NULL` for the others.

A `DynamicField` holds a value of any type, kept with the type its Python value has (an int as `Int64`, a
text as `String`, a list as an `Array`) and read back as it was written. `max_types` caps the types kept
in columns of their own. A filter matches the values of the filter value's type only (`value=5` doesn't
match `"5"`); a path named by a type reads the values of that type — `value__String__startswith="a"`,
`value__Int64__gt=3`.

ClickHouse orders no rows by a `Variant` or `Dynamic` column — `order_by()` on one raises
`UnSupportedError`; order by a path of it. The column holds `NULL` itself: its fields take no
`null=True`.

## <a id="geo"></a>Geo

A `GeometryField` of `hare.gis` is a `Point`, `LineString`, `Polygon`, `MultiLineString` or
`MultiPolygon` column, x and y only, with no SRID — a value read takes the field's. A point is a tuple,
which has no empty value: a point field with `null=True` raises `UnSupportedError`; a line or an area
written as `None` is stored empty. A polygon's rings are stored in the orientation ClickHouse's functions
read — the outer ring clockwise — so a polygon given the other way round reads back with its rings
reversed.

| `hare.gis` | ClickHouse |
|---|---|
| A point in an area (`__intersects`, `__within`, `__contains`, `__covers`, `__covered_by`, `__disjoint`) | `pointInPolygon` — a point on the border counts as in the area |
| Relations of two areas | `polygonsIntersect*`, `polygonsWithin*`, `polygonsEquals*` |
| `Area`, `Perimeter`, `ConvexHull`, `Intersection`, `Union`, `SymmetricDifference` of areas | `polygonArea*`, `polygonPerimeter*`, `polygonConvexHullCartesian`, `polygons*` |
| `Distance` of points | `L2Distance`; `geoDistance` in meters for a geography |
| `__dwithin` | the distance compared with the one given |
| `Distance` of areas | `polygonsDistance*` |
| `AsText` | `wkt` |

A geography (`GeometryField(geography=True)`) is measured on a sphere of WGS 84's mean radius with the
`*Spherical` functions — meters and square meters, a little off PostGIS's ellipsoid over long distances;
a function ClickHouse has only on the plane (`ConvexHull`, `polygonsEquals`) raises `UnSupportedError`
for it, as does anything else ClickHouse has no function of.
