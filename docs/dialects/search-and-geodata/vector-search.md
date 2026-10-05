# Vector search

Vector columns and similarity search, with one API on every dialect that has vector search — each
dialect stores the vectors and writes the distances its own way. ClickHouse has none through hare
(`Features.supports_vector_search` is off): a vector distance and `__nearby` raise `UnSupportedError` there.

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Engine | the [pgvector](https://github.com/pgvector/pgvector) extension | the [sqlite-vec](https://github.com/asg017/sqlite-vec) extension |
| Column | `vector(N)` | sqlite-vec's float32 `BLOB` |
| Dimensions | 1 to 16000 | 1 to 8192 |
| Distances | pgvector's operators `<->`, `<=>`, `<#>` | `vec_distance_l2()`, `vec_distance_cosine()`, a negative inner product hare registers |
| Indexes | `HnswIndex`, `IvfflatIndex` (ANN) | none — every row is scanned |
| Setup | none — the migration autodetector adds `CreateExtension("vector")` | `pip install hare-orm[sqlite-vec]` and `load_sqlite_vec=true` on the connection |

The distances and `__nearby` need `Features.supports_vector_search`; on a connection without it they
raise `UnSupportedError` before any SQL.

```python
from hare.vectors import CosineDistance, InnerProduct, L2Distance, VectorField


class Item(Model):
    embedding = VectorField(dimensions=384)


await Item.objects.annotate(distance=CosineDistance("embedding", query_vector)).order_by("distance")[:10]
await Item.objects.filter(embedding__nearby=(query_vector, 0.5))
```

## <a id="the-field"></a>The field

```python
VectorField(dimensions: int, **kwargs)
```

A fixed-length list of floats. `dimensions` is an `int` from 1 to 16000 (`ConfigurationError`
otherwise); a dialect taking fewer — SQLite takes 8192 — raises `UnSupportedError` when the column is
created or a value written. The Python value is a plain `list[float]`; each element must be finite and
fit a float32 (`float4` on PostgreSQL), and a value of another length raises `ValidationError` too.

On PostgreSQL a value reads back as the shortest decimal of its `float4` (`0.1`, not
`0.10000000149011612`) on both drivers. On SQLite values are written and read without the
extension — only the distances need it.

## <a id="distances"></a>Distances

Three distance expressions, each usable in `.annotate()`/`.order_by()`/`.filter()` like any other
annotation, comparing a field (a name, or an already-built `Term`/`Expression`) with a query vector
(a plain `list[float]`, or another field or expression). Smaller is more similar for each of them:

| Expression | Meaning |
| --- | --- |
| `L2Distance(field, vector)` | Euclidean distance |
| `CosineDistance(field, vector)` | Cosine distance |
| `InnerProduct(field, vector)` | The **negative** inner product — so smaller still means "more similar", consistent with the other two |

```python
results = (
    await Item.objects.annotate(dist=L2Distance("embedding", query_vector))
    .order_by("dist")
    .limit(10)
)
```

A query vector is converted the way the dialect stores the field it is compared with.

The three are `VectorDistanceExpression`s (`hare.vectors`), each declaring its `VectorDistanceType`
(`L2`, `COSINE`, `NEGATIVE_INNER_PRODUCT`) — what a dialect turns into its own operator or function.

## <a id="nearby"></a>`__nearby`

`field__nearby=(vector, max_distance)` — the rows within an L2 distance of `vector`, the same as an
`L2Distance` annotation filtered by `__lte`. A value that isn't a pair, or a distance that isn't a
finite number, raises `ValidationError`:

```python
await Item.objects.filter(embedding__nearby=(query_vector, max_distance))
```

## <a id="postgresql"></a>PostgreSQL: pgvector

pgvector is a database extension, not a Python package — nothing to add to `pyproject.toml`. The
migration autodetector adds a `CreateExtension("vector")` for it automatically wherever `VectorField`
is used, with no `Meta.extensions` declaration (see
[Migration operations — extensions](../../migrations/operations.md#extensions)).

**Indexes** — `IvfflatIndex(*expressions, lists: int = 100, ...)` and `HnswIndex(*expressions, m: int = 16,
ef_construction: int = 64, ...)` from `hare.dialects.postgresql.indexes`, alongside the generic
PostgreSQL indexes described on [Indexes](../../models/indexes.md#postgresql-index-types), both accept
`opclasses=` to pick the distance the index supports (`vector_l2_ops`, `vector_cosine_ops`,
`vector_ip_ops`). `HnswIndex(fields=...)` requires it — pgvector has no default operator class for
HNSW, so leaving it out raises `ConfigurationError`; `IvfflatIndex` defaults to `vector_l2_ops`. The
storage parameters are checked against pgvector's limits — `lists` from 1 to 32768, `m` from 2 to 100,
`ef_construction` from 4 to 1000 and at least `2 * m`; anything else (a `bool`, a string, an
out-of-range number) raises `ConfigurationError`:

```python
from hare.dialects.postgresql.indexes import HnswIndex


class Item(Model):
    embedding = VectorField(dimensions=1536)

    class Meta:
        indexes = (HnswIndex(fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_cosine_ops",)),)
```

> [!WARNING]
> **WITH before WHERE**
>
> If you also combine one of these with `condition=` (a partial index), the *storage parameters*
> (`WITH (lists=...)` / `WITH (m=..., ef_construction=...)`) must come **before** the *condition*
> (`WHERE (...)`) in the generated `CREATE INDEX` statement — that's a hard PostgreSQL syntax
> requirement, not a style choice. `IvfflatIndex`/`HnswIndex` already get this ordering right on
> their own; this note exists to explain *why*, not because you need to do anything about it: if
> you're extending either class yourself, remember `PartialIndex.__init__` only ever *appends*
> its `WHERE` clause, so anything adding its own storage parameters must *prepend* them instead.

## <a id="sqlite"></a>SQLite: sqlite-vec

Install the `sqlite-vec` extra (`pip install hare-orm[sqlite-vec]`) and load the extension on the
connection:

```python
DB_URL = "sqlite+aiosqlite://db.sqlite3?load_sqlite_vec=true"
```

- `load_sqlite_vec=true` loads the extension into every connection of the pool; without the package
  installed, setting the connection up raises `ConfigurationError`. Without the option the connection
  has no `supports_vector_search`, so a distance or `__nearby` raises `UnSupportedError` before any SQL.
- `L2Distance` and `CosineDistance` are sqlite-vec's `vec_distance_l2()`/`vec_distance_cosine()`;
  `InnerProduct` is computed by a function hare registers, as sqlite-vec has none. A distance is NULL
  when a vector is NULL.
- sqlite-vec scans every row (no ANN index) — right for up to a few hundred thousand vectors.
