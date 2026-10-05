# Indexes

The indexes a model declares in `Meta.indexes`: `Index`, `PartialIndex` and the index types of a dialect.
Migrations create, change and drop them with the model.

```python
from hare.ddl import Index, PartialIndex, RawSQLTerm
from hare.dialects.postgresql.indexes import GinIndex  # and the other PostgreSQL index types
from hare.query.expressions import Q
from hare.query.functions import Lower


class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=300)
    isbn = fields.CharField(max_length=13)
    published_at = fields.DateField(null=True)

    class Meta:
        indexes = (
            Index(fields=("-published_at", "title")),
            Index(Lower("title"), name="book_title_lower"),
            PartialIndex(fields=("isbn",), condition=Q(published_at__isnull=False), unique=True),
        )
```

## <a id="index"></a>`Index`

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
    ) -> None: ...
```

An index has keys of `fields` or of `expressions` — one of them, not both, or `ConfigurationError`.
`Meta.indexes` also takes a plain tuple of field names — an index over their columns, its name generated,
as `Index(fields=...)` gives.

## <a id="key-order"></a>Key order and NULL placement

A key is ascending unless its field name starts with `-`: `Index(fields=("-published_at", "title"))` is
`(published_at DESC, title)`. For an explicit NULL placement, spell a key as an ordering —
`Index(F("rating").desc(nulls_last=True), name="book_rating_nulls_last")`,
`Index(F("rating").asc(nulls_first=True), F("price"), name=...)`; `F("a")`/`F("a").desc()` keys with no
NULL placement are the same index as `fields=("a",)`/`("-a",)`. SQLite indexes can't place NULLs (they
keep them first ascending, last descending) and raise `UnSupportedError` for it when the index is
created.

## <a id="expression-keys"></a>Expression keys

A key may be an expression, with a direction and a NULL placement as above:
`Index(Lower("title").desc(nulls_last=True), name="book_title_lower_desc")`. A collated key is an
expression too: `Index(Collate("title", "C"), name="book_title_c")`. Each database writes an expression
key in its own SQL — `Index(Trunc("published_at", "month"))`, `Index(Extract("published_at", "year"))`,
`Index(Cast("price", IntField()))`, `Index(F("details__edition"))` too; an unnamed one is named after its
keys, the same name on every database.

A key whose SQL needs one of the functions hare installs on its own SQLite connections (a truncated or
extracted date part, a cast) raises `UnSupportedError` when the index is created on SQLite: the database
keeps an index up to date without hare, and any other client writing the table couldn't (see [In
constraints and indexes](../dialects/sqlite/functions.md#in-constraints-and-indexes)).

A `UniqueConstraint` takes field names; an expression is unique through
`Index(<expression>, unique=True)`.

## <a id="include"></a>Non-key columns: `include`

`include` names fields stored in the index as non-key columns (PostgreSQL `INCLUDE`), so a query reading
only them and the key columns is answered from the index alone; SQLite has no such columns and creates
the index without them. Btree, GiST and SP-GiST indexes take it —
`GinIndex`/`BrinIndex`/`HashIndex`/`BloomIndex`/`IvfflatIndex`/`HnswIndex` raise `UnSupportedError` for
it.

```python
class Meta:
    indexes = (Index(fields=("author",), include=("title", "published_at")),)
```

## <a id="opclasses"></a>Operator classes: `opclasses`

`opclasses` gives each field of `fields` its operator class, so it needs `fields` and one class per field
(`ConfigurationError` otherwise) — e.g. an index serving `LIKE` by a prefix on PostgreSQL:

```python
class Meta:
    indexes = (Index(fields=("isbn",), opclasses=("varchar_pattern_ops",)),)
```

## <a id="partial-index"></a>`PartialIndex`

```python
class PartialIndex(Index):
    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        # condition: Q - a condition over the model's fields; RawSQLTerm - a raw WHERE predicate
        condition: Q | RawSQLTerm | None = None,
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None: ...
```

An index of the rows matching `condition`. A `Q` condition reads the model's own fields, as a
constraint's does — see [Constraints](constraints-and-triggers.md#constraints). A `RawSQLTerm` condition
is written as the `WHERE` clause as it is — for what a `Q` can't say (a function call, a
database-specific operator):

```python
PartialIndex(fields=("title",), condition=RawSQLTerm("length(title) > 100"))
```

A database without partial indexes (`Features.supports_partial_indexes` — ClickHouse) gets the index of
every row: it serves the queries the partial one serves, and `hare drift` takes it for the declared
index. A unique index or `UniqueConstraint` with a condition is not created there — see
[ClickHouse models](../dialects/clickhouse/models.md).

## <a id="index-types"></a>Index types of a dialect

PostgreSQL has a family of index types in `hare.dialects.postgresql.indexes` —
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/`HashIndex`/`SpGistIndex` — all subclasses of
`PartialIndex` with the same constructor, so every one of them takes `condition=` too. Only a plain index
can be unique: `unique=True` on an index type raises `UnSupportedError`. See [PostgreSQL index
types](../dialects/postgresql/indexes-and-tables.md#postgresql-index-types).

## <a id="index-names"></a>Index names

An index without `name=` gets one generated from its table and columns — plus, when set, its access
method, operator classes, `include` columns, storage parameters and condition, so several unnamed indexes
on the same columns (a GiST and an SP-GiST one, two HNSW ones with different operator classes, a plain
and a partial one) each get their own name. A plain btree index's name depends on its table and columns
only.

A model whose indexes would share a name raises `ConfigurationError` as it is defined: the same unnamed
index declared twice, or one `name=` given to two indexes — or to an index and a unique or exclusion
constraint, which the database keeps as indexes too. So does a unique index next to a `UniqueConstraint`
over the same fields with the same condition (or none) — one rule declared twice; with different
conditions (`PartialIndex(fields=["isbn"], condition=Q(in_print=True), unique=True)` and
`UniqueConstraint(fields=["isbn"], condition=Q(archived=False))`) they are two rules and both are kept.
