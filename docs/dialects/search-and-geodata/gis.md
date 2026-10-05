# Geographic data (GIS)

`hare.gis` stores points, lines and areas in spatial columns, filters rows by how their geometries
relate to others, and computes areas, distances and new geometries in the database. On PostgreSQL it
runs on [PostGIS](https://postgis.net/); the migration autodetector adds
`CreateExtension("postgis")` wherever a geometry field is used. On SQLite it runs on
[SpatiaLite](https://www.gaia-gis.it/fossil/libspatialite/), through the same API — see
[SQLite: SpatiaLite](#spatialite). On ClickHouse a geometry field is one of its geo types — points,
lines and areas with x and y only and no SRID — and the lookups and functions it has a function of run
there; see [ClickHouse: Geo](../clickhouse/types.md#geo). On a database without spatial types a geometry field raises
`UnSupportedError` when its column type is asked for, and every spatial lookup, function and aggregate
raises it while the query is built — before any SQL is sent (`Features.supports_spatial`).

```python
from hare import Model, fields
from hare.dialects.postgresql.indexes import GistIndex
from hare.gis import Point, PointField, PolygonField


class Shop(Model):
    name = fields.CharField(max_length=100)
    location = PointField()                    # geometry(Point,4326)
    delivery_area = PolygonField(null=True)    # geometry(Polygon,4326)

    class Meta:
        indexes = (GistIndex(fields=("location",)), GistIndex(fields=("delivery_area",)))


await Shop.objects.create(name="Corner", location=Point(37.6173, 55.7558))
nearby = await Shop.objects.filter(location__dwithin=(Point(37.62, 55.75), 0.01))
```

Spatial lookups use an index only when the column has one — declare a `GistIndex` (PostgreSQL) or a
[`SpatialiteIndex`](#spatial-index) (SQLite) for every geometry column you filter on. Each is an index
of its own database: on the other one it raises `UnSupportedError` before any SQL, whether the schema
is generated or a migration adds or drops it.

## <a id="geometries"></a>Geometry values

A geometry field holds a `hare.gis.Geometry` — an immutable value compared by type, SRID and
coordinates:

| Class | Example |
|---|---|
| `Point(x=None, y=None, z=None, *, srid=None)` | `Point(37.6173, 55.7558)` — for longitude/latitude, x is the longitude; `Point()` is the empty point |
| `LineString(positions=(), *, srid=None)` | `LineString([(0, 0), (1, 1), (2, 0)])` — two or more positions |
| `Polygon(exterior=(), holes=(), *, srid=None)` | `Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)], holes=[[(1, 1), (2, 1), (2, 2), (1, 1)]])` — every ring closed, four or more positions |
| `MultiPoint(members=(), *, srid=None)` | `MultiPoint([(0, 0), Point(1, 1)])` |
| `MultiLineString(members=(), *, srid=None)` | `MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])` |
| `MultiPolygon(members=(), *, srid=None)` | `MultiPolygon([polygon, [exterior_ring, hole_ring]])` |
| `GeometryCollection(members=(), *, srid=None)` | `GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])` |

A position is a `Point` or a sequence of two or three finite numbers; the positions of one geometry
all have the same number of coordinates. A member of a multi geometry is a geometry of its type or
its coordinates, and its own SRID is dropped — the collection's applies. Every class checks its
arguments: a wrong shape, a non-finite number, an unclosed ring, a member of another type raise
`ValidationError`. Measured (M) coordinates aren't supported.

Each geometry has:

- `wkt` — its well-known text without the SRID (`POINT Z (1 2 3)`), `ewkt` — with it
  (`SRID=4326;POINT (1 2)`);
- `__geo_interface__` — the GeoJSON geometry object (`{"type": "Point", "coordinates": [1.0, 2.0]}`),
  which shapely (`shapely.geometry.shape(point)`) and other libraries read;
- `srid`, `is_empty`, `has_z`, `get_coordinates()` (nested tuples, as GeoJSON nests them), and the
  coordinates of its class — `point.x`, `polygon.exterior`, `polygon.holes`, `collection.members`;
- `with_srid(srid)` — a copy with another SRID (the coordinates are not transformed);
- `from_coordinates(coordinates, srid=None)` — the geometry of GeoJSON-style coordinates.

`hare.gis.readers` reads other forms: `WktReader.read(text)` (WKT or EWKT), `EwkbReader.read(data)`
((E)WKB bytes or their hex text — PostGIS's extended form with an SRID and ISO WKB) and
`GeoJsonReader.read(value)` (a GeoJSON mapping or any object with `__geo_interface__`). Each raises
`ValidationError` for a value that isn't a whole geometry.

## <a id="fields"></a>Fields

```python
GeometryField(geometry_type=None, *, srid=4326, geography=False, dimensions=2, **kwargs)
```

| Argument | Meaning |
|---|---|
| `geometry_type` | The type of geometry the column holds — a `GeometryType` (`POINT`, `POLYGON`, ...) or its name; any type (`GEOMETRY`) when not given |
| `srid` | The spatial reference system, an `int` from 1 to 999999 — 4326 (WGS 84 longitude/latitude) by default |
| `geography` | `True` for a `geography` column — longitude/latitude on the spheroid, measured in meters; `False` for a planar `geometry` |
| `dimensions` | 2 for x/y, 3 for x/y/z |

A wrong argument raises `ConfigurationError` when the model is declared. `PointField`,
`LineStringField`, `PolygonField`, `MultiPointField`, `MultiLineStringField`, `MultiPolygonField` and
`GeometryCollectionField` are `GeometryField`s holding one type. On PostgreSQL the column is
`geometry(<Type>[Z],<srid>)` or `geography(<Type>[Z],<srid>)`.

A field takes a value in any of these forms, in a write, on assignment and in a filter:

- a `Geometry`;
- WKT or EWKT text — `"POINT(1 2)"`, `"SRID=4326;POINT(1 2)"`;
- (E)WKB bytes, or their hex text;
- a GeoJSON geometry mapping, or any object with `__geo_interface__` (a shapely geometry).

A geometry without an SRID gets the field's, so `Point(1, 2)` assigned to a `PointField()` is held as
`Point(1, 2, srid=4326)` — memory holds what a read returns. A written geometry must be of the
field's type, have its dimensions and its SRID; otherwise `ValidationError` is raised before the query
runs. A geometry in another SRID is not transformed on write — transform it first, or store it in a
column of its own SRID. Values are read back as `Geometry` objects carrying their SRID.

### <a id="geography"></a>Choosing geometry or geography

A `geometry` column computes on the plane of its SRID: on 4326 an area is in square degrees and a
distance in degrees. A `geography` column (`geography=True`) computes on the spheroid: areas, lengths
and distances are in square meters and meters, for any two points on Earth. PostGIS tests only some
relations on a geography — `intersects`, `covers`, `covered_by`, `dwithin`, `bbox_overlaps` and the
distance lookups; any other relation lookup on a geography raises `UnSupportedError`, as do the
spatial aggregates. Functions without a geography form (`Envelope`, `NumPoints`, the `x`/`y` paths,
...) read a geography's longitude/latitude as a geometry. A planar SRID in meters (for example 3857, or
a local projection) is the alternative when every point is in one region.

## <a id="lookups"></a>Lookups

A geometry field keeps equality (`location=...`, `__not`) and `__isnull`/`__not_isnull`; there are no
comparisons or text lookups. The spatial lookups take a geometry in any form the field takes — or
`F()` of another geometry column:

| Lookup | True when the column's geometry |
|---|---|
| `__intersects` | shares a point with the value |
| `__contains` | holds the value, its boundary aside (a point on the edge is not contained) |
| `__contains_properly` | holds the value without touching its boundary |
| `__within` | lies inside the value |
| `__covers` | holds the value, its boundary included |
| `__covered_by` | lies inside the value or on its boundary |
| `__crosses` | crosses it (a line through a polygon, two lines in one point) |
| `__disjoint` | shares no point with it |
| `__equals` | is the same set of points (a ring may start at another vertex) |
| `__overlaps` | overlaps it, each having a part outside the other (same dimension) |
| `__touches` | touches it only on the boundaries |
| `__bbox_overlaps` | has a bounding box overlapping the value's |
| `__bbox_contains` | has a bounding box holding the value's |
| `__bbox_contained` | has a bounding box inside the value's |
| `__relate=(geometry, pattern)` | matches the DE-9IM pattern — nine of `0`, `1`, `2`, `T`, `F`, `*` |
| `__isvalid=True/False` | is (not) valid — closed rings, no self-intersection, ... |

```python
await Shop.objects.filter(delivery_area__contains=customer_point)
await Shop.objects.filter(location__within=F("district__boundary"))
await Parcel.objects.filter(shape__relate=(road, "T********"))
```

The distance lookups take `(geometry, distance)` — a finite number of zero or more, in meters on a
geography and in the SRID's units on a geometry:

| Lookup | True when the distance from the column's geometry to the value |
|---|---|
| `__dwithin` | is at most `distance` — uses a spatial index |
| `__distance_lt`, `__distance_lte` | is below / at most `distance` |
| `__distance_gt`, `__distance_gte` | is above / at least `distance` |

```python
await City.objects.filter(center__dwithin=(Point(37.62, 55.75), 10_000))   # geography: 10 km
```

`__dwithin` is the one to filter by radius with: the distance comparisons compute the distance of
every row. A filter geometry in another SRID than the column's is transformed to the column's SRID
in the query. A wrong value — not a geometry, not a pair, a negative distance, a wrong pattern —
raises `ValidationError` before the query runs.

## <a id="paths"></a>Paths

`__x`, `__y`, `__z` read a point's coordinates (a float) and `__srid` the SRID (an int) — in filters,
`values()`, `F()` and `order_by()`:

```python
await Shop.objects.filter(location__x__gte=37.5).values_list("location__y", flat=True)
```

## <a id="functions"></a>Functions

`hare.gis.functions` — each takes a geometry as its first argument (a field name, `F()` or an
expression); a second geometry (`Distance`, `Intersection`, ...) is `F()`, an expression or a
geometry from Python in any form a field takes, transformed to the first one's SRID:

| Function | Result |
|---|---|
| `Area(geometry)` | float — square meters on a geography, the SRID's units on a geometry |
| `Length(geometry)`, `Perimeter(geometry)` | float — a line's length, a polygon's rings' length |
| `Distance(geometry, other)` | float — the shortest distance |
| `Centroid`, `PointOnSurface` | a point — the center of mass, a point surely on the geometry |
| `Envelope`, `Boundary`, `ConvexHull` | the bounding box, the boundary, the convex hull |
| `Buffer(geometry, distance)` | the area within `distance` (negative shrinks a polygon) |
| `Intersection`, `Difference`, `SymmetricDifference`, `Union` (of two) | the overlay of two geometries |
| `Transform(geometry, srid)` | the geometry in another SRID |
| `Simplify(geometry, tolerance)` | fewer points, none moved further than `tolerance` |
| `MakeValid(geometry)` | a valid geometry covering an invalid one's points |
| `IsValid(geometry)` | bool |
| `AsText(geometry)` | str — WKT |
| `AsGeoJSON(geometry)` | dict — the GeoJSON geometry |
| `NumPoints(geometry)`, `NumGeometries(geometry)` | int |
| `GeometryTypeName(geometry)` | str — `POINT`, `POLYGON`, ... |

```python
from hare.gis.functions import Area, Distance, Transform

await City.objects.annotate(km=Distance("center", moscow) / 1000).order_by("km")
await Parcel.objects.annotate(square_meters=Area(Transform("shape", 3857)))
```

A geometry result is read as a `Geometry`; `Buffer`'s distance, `Simplify`'s tolerance and
`Transform`'s SRID are checked when the function is built (`ValidationError`).

## <a id="aggregates"></a>Aggregates

`hare.gis.aggregates` merge the geometries of a group (`distinct=`, `_filter=` as for any aggregate);
geometry columns only:

| Aggregate | Result |
|---|---|
| `Collect(geometry)` | the geometries as one multi geometry or collection, unmerged |
| `UnionAggregate(geometry)` | the geometries merged into one, overlaps dissolved |
| `MakeLine(point, order_by=...)` | the points as one line, in `order_by` order |
| `Extent(geometry)` | the bounding box as `(xmin, ymin, xmax, ymax)` |

```python
from hare.gis.aggregates import Extent, MakeLine

await Shop.objects.aggregate(bounds=Extent("location"))
await Ping.objects.values("vehicle").annotate(track=MakeLine("position", order_by="created_at"))
```

## <a id="spatialite"></a>SQLite: SpatiaLite

On SQLite a geometry is stored in SpatiaLite's own BLOB format, which hare writes and reads itself:
storing, reading and comparing geometries for equality need no extension. The spatial lookups, the
`__x`/`__y`/`__z`/`__srid` paths, the functions and the aggregates run on the SpatiaLite extension,
loaded into every connection by the connection's settings:

```python
DB_URL = "sqlite+aiosqlite://db.sqlite3?load_spatialite=true"
```

| Setting | Meaning |
|---|---|
| `load_spatialite` | `true` loads SpatiaLite into every connection — `Features.supports_spatial` |
| `spatialite_path` | The library: a name the system's loader finds (`mod_spatialite`, the default — Debian/Ubuntu's `libsqlite3-mod-spatialite`, Homebrew's `libspatialite`) or a path. On Windows a path with a directory also loads the libraries SpatiaLite needs (GEOS, PROJ, ...) from that directory |
| `spatialite_proj_database` | The path of PROJ's `proj.db`, which a transformation between SRIDs reads — needed when PROJ doesn't find its own (the Windows build carries it next to the library) |
| `spatialite_metadata` | The spatial metadata hare creates in a database without any, when a connection loads SpatiaLite: `wgs84` (the default) — the WGS84 reference systems, longitude/latitude 4326 and its UTM zones, created in milliseconds; `full` — every EPSG reference system SpatiaLite knows, slower to create; `none` — no metadata. A database that already has metadata keeps its own. A geography (`Features.supports_geography`) and a spatial index (`Features.supports_spatial_index`) need it |

`spatialite_path`, `spatialite_proj_database` and `spatialite_metadata` without `load_spatialite=true`
raise `ConfigurationError`, as do a library that doesn't load and a `spatialite_metadata` other than
the three. Without `load_spatialite` every spatial lookup, path, function and aggregate raises
`UnSupportedError` before any SQL.

The reference systems are read from the metadata when the connection opens
(`Features.spatial_reference_ids`). A geography or a spatial index in an SRID the metadata hasn't
raises `UnSupportedError` before any SQL — with `wgs84` metadata, for one, a geography in NAD83 (4269)
or a spatial index of a Web Mercator (3857) column: create the database with `spatialite_metadata=full`
or add the reference system to it (SpatiaLite's `InsertEpsgSrid()`), and connect again.

- **Columns** are typed by the geometry type — `POINT`, `POLYGON`, `GEOMETRY`, with a `Z` for x/y/z
  (`POINTZ`). SpatiaLite has no empty geometry: writing one raises `ValidationError`.
- **A geography** is a longitude/latitude geometry measured on the ellipsoid: `Area`, `Length`,
  `Perimeter`, `Distance`, `__dwithin` and the distance lookups in square meters and meters, read
  through SpatiaLite's spatial metadata — with `spatialite_metadata=none` they raise
  `UnSupportedError`. The relations PostGIS tests on the spheroid (`__intersects`, `__covers`,
  `__covered_by`, `__bbox_overlaps`) and `Centroid`, `Buffer`, `Intersection` of a geography raise
  `UnSupportedError` — SpatiaLite has no such form; transform it to a planar SRID first. SpatiaLite
  measures on the ellipsoid with librttopo, the PostGIS geometry library's fork.
- **Transforming between SRIDs** (`Transform`, a filter geometry in another SRID) names the SRIDs as
  EPSG codes for PROJ, so the database needs no reference system table for it.
- **`__contains_properly`** is the DE-9IM pattern `T**FF*FF*` — SpatiaLite has no function of its own.
- **`GeometryTypeName`** gives `POINT` for a point with z, as PostGIS does (SpatiaLite says `POINT Z`).
- **`MakeLine(order_by=...)`** needs SQLite 3.44 or later (`Features.supports_ordered_aggregates`).
- SpatiaLite's spatial metadata tables are no model's — `inspectdb` and the drift check leave them out.

### <a id="spatial-index"></a>Spatial index

`SpatialiteIndex` is SpatiaLite's spatial index of one geometry field — an R*Tree of the bounding
boxes of the column's geometries:

```python
from hare import Model, fields
from hare.dialects.sqlite.indexes import SpatialiteIndex
from hare.gis import PointField


class Shop(Model):
    id = fields.IntField(primary_key=True)
    location = PointField()

    class Meta:
        indexes = (SpatialiteIndex(fields=("location",)),)
```

- **Creating it** registers the column in the spatial metadata with the field's geometry type, SRID
  and dimensions (`RecoverGeometryColumn()`) and creates its R*Tree (`CreateSpatialIndex()`), filled
  from the rows already there. From then on SpatiaLite's triggers keep the R*Tree in step with the
  table and refuse a geometry of another type, SRID or dimensions with `IntegrityError` — rows written
  through hare never have one, as a geometry field checks them first. A migration adding the index
  to a table holding such a row — written by other SQL — fails with `OperationalError`.
- **Its name** is SpatiaLite's: the R*Tree table is `idx_<table>_<column>`, so the index takes no
  `name`. One index per field.
- **The model needs an integer primary key** — the R*Tree keys its rows by it, as SQLite may renumber
  the implicit rowid of a table without one; otherwise `ConfigurationError`.
- **Lookups**: `__intersects`, `__contains`, `__contains_properly`, `__within`, `__covers`,
  `__covered_by`, `__crosses`, `__equals`, `__overlaps`, `__touches`, the `__bbox_*` lookups and a
  geometry's `__dwithin` first take the rows whose bounding box meets the other geometry's — the box
  grown by the distance for `__dwithin` — from the R*Tree (SpatiaLite's `SpatialIndex` table), and
  test only those; through a relation (`shops__location__within=...`) too. `__disjoint`, `__relate`,
  the distance lookups and a geography's `__dwithin` (its distance is in meters, the boxes in degrees)
  test every row. The R*Tree is read only when the model declares the index — a database that lost
  it (dropped by other SQL) answers these lookups with no rows: the drift check reports it missing.
- **Migrations** keep it in step: renaming the table or the field, a table rebuild (altering another
  field, `STRICT`), and dropping the model drop the R*Tree and the column's registration first and
  create them again for the new table or column. `sqlmigrate` shows the SpatiaLite calls.
- **The drift check and `inspectdb`** read it from the spatial metadata (an enabled spatial index of
  the column); its R*Tree tables and SpatiaLite's triggers are no model's.
- It needs `features.supports_spatial_index` — SpatiaLite loaded with its metadata — and the field's
  SRID in the metadata; otherwise `UnSupportedError` before any SQL. On PostgreSQL it raises
  `UnSupportedError` — declare a `GistIndex` there.

## <a id="integrations"></a>Pydantic and inspectdb

In a pydantic model generated from a model, a geometry field is a GeoJSON geometry: it accepts a
GeoJSON mapping, WKT/EWKT, (E)WKB or a `Geometry`, serializes as the GeoJSON object, and its JSON
schema describes a GeoJSON geometry. `inspectdb` reads a PostGIS column with a type and an SRID as
the matching field (`geometry(PolygonZ,3857)` -> `PolygonField(srid=3857, dimensions=3)`); a
`geography(Point,4326)` column stays the [`PostGISField`](../postgresql/fields.md#postgisfield) of a
`(latitude, longitude)` tuple, and a column without a type and an SRID, or with measured coordinates,
is an ambiguous `TextField`.
