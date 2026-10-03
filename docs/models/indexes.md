# Indexes

```python
class Index:
    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None
```

A key is ascending unless its field name starts with `-`, as in Django:
`Index(fields=("-created_at", "author"))` is `(created_at DESC, author)`. For an explicit NULL placement
spell a key as an ordering - `Index(F("rank").desc(nulls_last=True), name="rank_nulls_last")`,
`Index(F("rank").asc(nulls_first=True), F("score"), name=...)`; `F("a")`/`F("a").desc()` keys with no NULL
placement are the same index as `fields=("a",)`/`("-a",)`. SQLite indexes can't place NULLs (they keep
them first ascending, last descending) and raise `ConfigurationError` for it. A collated key is an
expression: `Index(Collate("title", "C"), name="title_c")`.

`include` names fields stored in the index as non-key columns (Postgres `INCLUDE`), so a query reading
only them and the key columns is answered from the index alone; SQLite has no such columns and creates
the index without them. Btree, GiST and SP-GiST indexes take it - `GinIndex`/`BrinIndex`/`HashIndex`/
`BloomIndex`/`IvfflatIndex`/`HnswIndex` raise `UnSupportedError` for it.

```python
class Meta:
    indexes = (Index(fields=("customer",), include=("total", "created_at")),)
```

`fields` and `expressions` are mutually exclusive; at least one is required. `opclasses` requires
`fields`, and must match its length — e.g. for a `LIKE`-prefix-friendly index on Postgres:

```python
class Meta:
    indexes = (Index(fields=("path",), opclasses=("varchar_pattern_ops",)),)
```

```python
class PartialIndex(Index):
    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        condition: Q | RawSQLTerm | None = None,  # Q: a condition over the model's fields; RawSQLTerm: a raw WHERE predicate
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None
```

A `RawSQLTerm` condition is written as the `WHERE` clause as it is — for what a `Q` can't say (a
function call, a database-specific operator):

```python
PartialIndex(fields=("status",), condition=RawSQLTerm("status IN ('open', 'in_progress')"))
```

Postgres also has a family of index types in `hare.dialects.postgresql.indexes` —
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/`HashIndex`/`SpGistIndex` — all subclasses of
`PartialIndex` with the identical constructor, so every one of them supports `condition=` too. See
[PostgreSQL index types](#postgresql-index-types).

An index without `name=` gets one generated from its table and columns - plus, when set, its access
method, operator classes, `include` columns, storage parameters and condition, so several unnamed indexes on the same
columns (a GiST and an SP-GiST one, two HNSW ones with different operator classes, a plain and a
partial one) each get their own name. A plain btree index's name depends on its table and columns only.

## PostgreSQL index types {: #postgresql-index-types }

`hare.dialects.postgresql.indexes` — all subclass `PartialIndex` (see above) with an identical constructor, so every
one of these also supports `condition=`:

`GinIndex`, `GistIndex`, `BrinIndex`, `BloomIndex`, `HashIndex`, `SpGistIndex`.

```python
class Widget(Model):
    values = fields.JSONField(default=list, field_type=list[str])

    class Meta:
        indexes = (GinIndex(fields=("values",)),)
```
