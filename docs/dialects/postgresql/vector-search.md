# Vector search (`hare.dialects.postgresql.fields.vector`, pgvector)

Requires the [pgvector](https://github.com/pgvector/pgvector) Postgres extension (a database
extension, not a Python package — nothing to add to `pyproject.toml`). The migration autodetector
adds a `CreateExtension("vector")` for it automatically wherever `VectorField` is used — no
`Meta.extensions` declaration needed (see
[Migration operations — extensions](../../migrations/operations.md#extensions)).

```python
VectorField(dimensions: int, **kwargs)
```

A fixed-length `vector(N)` column. The Python value is a plain `list[float]`; each element must be
finite and fit a `float4` (`ValidationError` otherwise), and reads back as the shortest decimal of its
`float4` (`0.1`, not `0.10000000149011612`) on both drivers:

```python
from hare.dialects.postgresql.fields.vector import VectorField
from hare.dialects.postgresql.functions.vector import CosineDistance, InnerProduct, L2Distance
from hare.dialects.postgresql.indexes import HnswIndex

class Item(Model):
    embedding = VectorField(dimensions=1536)

    class Meta:
        indexes = (HnswIndex(fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_cosine_ops",)),)
```

Three distance expressions, each usable in `.annotate()`/`.order_by()`/`.filter()` like any other
annotation, comparing a field (a name, or an already-built `Term`/`Expression`) against a query
vector (a plain `list[float]`, or another field/expression):

| Expression | Operator | Meaning |
| --- | --- | --- |
| `L2Distance(field, vector)` | `<->` | Euclidean distance |
| `CosineDistance(field, vector)` | `<=>` | Cosine distance |
| `InnerProduct(field, vector)` | `<#>` | **Negative** inner product — pgvector defines it this way so smaller still means "more similar", consistent with the other two |

```python
results = (
    await Item.objects.annotate(dist=L2Distance("embedding", query_vector))
    .order_by("dist")
    .limit(10)
)
```

A `nearby` lookup shorthand is also available, equivalent to an `L2Distance` annotation filtered
by `__lte`, for the common "everything within some distance" case without an explicit annotate:

```python
await Item.objects.filter(embedding__nearby=(query_vector, max_distance))
```

**Indexes** — `IvfflatIndex(*, lists: int = 100, ...)` and `HnswIndex(*, m: int = 16,
ef_construction: int = 64, ...)`, alongside the generic Postgres indexes described on
[Indexes](../../models/indexes.md#postgresql-index-types), both accept `opclasses=` to pick
which distance the index supports (`vector_l2_ops`, `vector_cosine_ops`, `vector_ip_ops`).
`HnswIndex(fields=...)` requires it — pgvector has no default operator class for HNSW, so
leaving it out raises `ConfigurationError`; `IvfflatIndex` defaults to `vector_l2_ops`.
The storage parameters are checked against pgvector's limits - `lists` from 1 to 32768, `m` from 2 to
100, `ef_construction` from 4 to 1000 and at least `2 * m`; anything else (a `bool`, a string, an
out-of-range number) raises `ConfigurationError`:

```python
class Meta:
    indexes = (IvfflatIndex(fields=("embedding",), lists=100, opclasses=("vector_l2_ops",)),)
```

!!! warning "WITH before WHERE"
    If you also combine one of these with `condition=` (a partial index), the *storage parameters*
    (`WITH (lists=...)` / `WITH (m=..., ef_construction=...)`) must come **before** the *condition*
    (`WHERE (...)`) in the generated `CREATE INDEX` statement — that's a hard Postgres syntax
    requirement, not a style choice. `IvfflatIndex`/`HnswIndex` already get this ordering right on
    their own; this note exists to explain *why*, not because you need to do anything about it: if
    you're extending either class yourself, remember `PartialIndex.__init__` only ever *appends*
    its `WHERE` clause, so anything adding its own storage parameters must *prepend* them instead.
