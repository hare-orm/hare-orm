# Aggregates, grouping and window functions

How an aggregate in `annotate()` groups the rows, when an aggregate next to a to-many relation would
count a row twice (hare raises `QueryError` there instead of returning an inflated number), grouping sets
with subtotals, and window functions with their frames. The aggregates themselves are on
[Database functions](functions.md#aggregates).

## <a id="implicit-group-by"></a>Implicit `GROUP BY`

The implicit `GROUP BY` of an aggregate `annotate()` follows these rules:

- An expression that contains an aggregate anywhere — `Count("books") + F("id")`,
  `Coalesce(Sum("books__pages"), F("fallback"))`, `F("book_count") * F("price")` over an aggregate
  annotation, a `Case` with an aggregate in one branch — is an aggregate expression: its filters go to
  `HAVING`, and the query is grouped by the columns it reads outside the aggregate.
- A selected non-aggregate expression is grouped by its `SELECT` position (`GROUP BY 2`), so a literal
  inside it (`F("price") + 5`, `Case(When(..., then=Value("high")))`, `Concat(...)`) binds once.
- A column `HAVING` reads outside its aggregates (`Q(book_count__gte=2) | Q(city="Oslo")`,
  `book_count__gt=F("limit")`) is grouped too when the aggregate is computed per model row.
  A query grouped by the fields named keeps those groups; other columns in `HAVING` remain
  ungrouped and are subject to the database's rules below.
- A correlated `Exists(...)`/`Subquery(...)` next to an aggregate is grouped as a whole, by its `SELECT`
  position, not by the outer columns its `OuterReference(...)` reads — two outer rows with different
  `OuterReference` values but the same subquery result land in one group. Only a subquery reading an
  outer aggregate is grouped by those columns.
- A window function (`Window(...)`) is never a group key — it is computed per group; the columns it reads
  outside its aggregates (its argument, `partition_by`, `order_by`) are grouped by too.
- A queryset of model instances (and its `count()`/`exists()`/`update()`/`delete()`) is always grouped by
  the primary key as well — one group per row. `values()`/`values_list()` group by exactly the fields
  named an aggregate annotated **after** them (`values("author_id").annotate(n=Count("id"))` — one row
  per author); an aggregate annotated **before** them, or passed to them as a keyword argument, is
  computed per model row, grouped by the primary key too
  (`annotate(n=Count("books")).values("country", "n")` — one row per author).
- `Meta.ordering` orders a query of model instances with an aggregate, and a `values()` query of
  aggregates annotated before it — its columns are grouped by too, which leaves one group per row. A
  query grouped by the fields named — `group_by()`, or an aggregate after `values()` — leaves it out: its
  columns would split the groups.
- A query grouped by the fields named that reads another column outside an aggregate — a selected field,
  an `order_by()` or a `HAVING` condition (`values("author_id").annotate(n=Count("id")).order_by("name")`)
  — is the database's to run: PostgreSQL and ClickHouse refuse it, SQLite reads the column from any one
  row of the group. Group by it too, or read it through an aggregate
  (`Max("name")`). Grouped by the primary key (`group_by("id")`), every column of the model is read — it
  is one per group (ClickHouse is told to group by it too).

## <a id="aggregates-to-many"></a>Aggregates and to-many filters

`annotate(n=Count("books")).filter(books__price__gt=6)` reuses the aggregate's own `JOIN`, so the count
covers only the books matching the filter — exactly as `filter(...).annotate(...)` does, whichever order
you call them in. A non-`distinct` aggregate is correct only while its own relation chain is the only
to-many `JOIN` of the query: a second to-many relation — another aggregate, a deeper hop such as
`Count("books__reviews")` next to `Sum("books__price")`, the aggregate's own `_filter=Q(awards__...)`, a
`.filter(awards__...)`, a non-aggregate annotation such as `F("awards__year")`/`Case(When(awards__...))`,
a `values("awards__year")` field or an `.order_by("awards__year")` — repeats each row per related row and
would silently inflate the numbers, so it raises `QueryError` instead; pass `distinct=True` to every
affected `Count()` (safe for a count of the relation's rows), move the aggregate into a `Subquery()`
annotation, or run the aggregates as separate queries — `distinct=True` doesn't fix `Sum()`/`Avg()`,
which would then drop equal values rather than repeated rows (`.exclude(awards__...)` is fine — it
becomes a `NOT EXISTS` subquery, not a `JOIN`). An `OuterReference("awards__id")` inside a
`Subquery()`/`Exists()` joins `awards` into the outer query and counts the same way. `Max()`/`Min()` (and
`BoolAnd()`/`BoolOr()`) can't be changed by a repeated row, so they need no `distinct=True`. An aggregate
over the base table's own columns (`Sum("advance")`, `Count("id")`) has no to-many chain of its own, so
any to-many `JOIN` of the query — another aggregate's, a `.filter(books__...)`'s, an annotation's, a
`values()` field's or the ordering's — raises the same `QueryError`; `Count("id", distinct=True)` is
fine, and so is `.distinct().aggregate(...)` or an `aggregate()` over an aggregate annotation, which run
over one row per base row. `aggregate()` on a model queryset discards ordering and annotations it does
not read, so their unused joins do not trigger this refusal. An aggregate on a `values()` queryset
operates on that queryset's selected rows.

A to-many field used as a `GROUP BY` key splits the rows into groups instead of repeating them inside
one: `.annotate(total=Sum("copies")).group_by("tags__name").values_list("tags__name", "total")` (or the
implicit grouping of `.values("tags__name").annotate(total=Sum("copies"))`) sums each tag's copies. A
base row counts once per related row carrying the key value — grouping authors by `books__in_print`
counts an author once per book in print, so count the authors themselves with
`Count("id", distinct=True)`. A key that is the relation's primary key or a non-nullable unique field
(`tags__id`), or keys covering every field of a non-nullable `UniqueConstraint` or composite primary key
of the related model, pin one related row per group (a nullable unique field doesn't — its NULL rows
share one group — unless the relation's first `.filter()` call rules NULL out on that same `JOIN`, e.g.
`tags__code__isnull=False` or `tags__code="x"`; `tags__code__in=["x", None]` keeps NULL, and a negated
condition, `~~Q(...)` too, is a subquery that rules nothing out on the `JOIN`), so an aggregate over
another to-many relation is correct next to it:
`values("tags__id", "tags__name").annotate(n=Count("reviews"))` counts each tag's reviews. Any other key
(`tags__name`) splits the rows only while no non-`distinct` aggregate reads another to-many relation: two
tags may share a name, and each repeats the reviews within that group, so `Count("reviews")` grouped by
`tags__name` alone raises — add the primary key to the key. Only the key's own `JOIN` splits the rows: a
second `.filter(tags__...)` call builds a separate `JOIN` over `tags`, which repeats the rows inside each
group, so it raises too — put the conditions into one `.filter()` call. A plain `F("tags__name")`
annotation or `.alias()` used as the key counts as that field. Grouping by an aggregate annotation raises
`FieldError` — also on a queryset of model instances, `count()` and `exists()`, which read no group.

A to-many `JOIN` a filter narrows to at most one related row per row repeats nothing, so an aggregate
next to it is exact: an equality with one value on the related primary key, on a unique field (nullable
too — an equality never matches `NULL`) or on every field of a unique set — `filter(tags=tag)`,
`filter(tags=1)`, `filter(tags__pk=1)`, `filter(tags__id=1)`, `filter(tags__slug="x")`,
`filter(tags__group=1, tags__rank=2)` — ANDed at the top of a `.filter()` call, or `__isnull=True` of a
reverse FK (`filter(books__isnull=True)`, also on its primary key or a non-nullable field), which leaves
one `NULL` row. `Book.objects.filter(tags=1).aggregate(total=Sum("price"))`,
`Author.objects.filter(books=1).annotate(n=Count("awards"))`, a sliced `aggregate()`, keyset `iterator()`
and `Subquery(Book.objects.filter(tags=OuterReference("pk")).values("id").annotate(n=Count("id")))` all
work. The condition narrows only the `JOIN` its own `.filter()` call reads (the first call over the
relation reads the one `values()`/`order_by()`/aggregates share; a later call builds its own), and only
its last to-many hop (`filter(books__tags=1)` still repeats each book). A many-to-many link counts only
when the through table can't link two rows twice — the automatic through table's unique index, or a
unique set of a `through=` model covering both keys. Anything else — `__in`, a range, a non-unique field,
part of a unique set, an `OR`, a many-to-many `__isnull`, `__isnull=False` — still raises. So does an
aggregate over the narrowed relation itself next to another to-many `JOIN`
(`filter(books=1).annotate(Sum("books__pages"), Count("awards"))`): the one book still repeats once per
award, and the sum would be inflated.

## <a id="grouping-sets"></a>Several groupings: `Rollup`, `Cube`, `GroupingSets`

`group_by()` takes one `Rollup`, `Cube` or `GroupingSets` (`hare.query.grouping`) to group the rows
several ways in one query — subtotals and totals beside the groups:

```python
from hare.query.functions import Count, Grouping
from hare.query.grouping import Cube, GroupingSets, Rollup

await Loan.objects.values("branch", "genre").annotate(total=Count("id")).group_by(Rollup("branch", "genre"))
# branch+genre groups, a subtotal per branch (genre NULL), the grand total (both NULL)
```

| Grouping | Groups by | SQL |
|---|---|---|
| `Rollup("branch", "genre")` | `(branch, genre)`, `(branch)`, every row | `GROUP BY ROLLUP(branch, genre)` |
| `Cube("branch", "format")` | every combination: both, each alone, every row | `GROUP BY CUBE(branch, format)` |
| `GroupingSets(("branch", "genre"), "format", ())` | each argument — a name, a sequence of names, `()` for every row | `GROUP BY GROUPING SETS ((branch, genre), (format), ())` |

- A name is a field, a related field path or an annotation, like in `group_by()`; plain names given
  beside the grouping set are in every grouping (`group_by("year", Rollup("branch", "genre"))` is
  `GROUP BY year, ROLLUP(branch, genre)`).
- A field a grouping leaves out is `NULL` in its rows. `Grouping(*fields)` tells such a `NULL` from a
  `NULL` value: `GROUPING()`, an `int` with a bit per field (the last field the lowest), set where the
  row's group leaves the field out — `Grouping("genre")` is 1 in the branch subtotals. It is computed per
  group, like an aggregate. It reads the fields the query groups by — of a plain one it is always 0;
  one of another field, or in a query grouped by nothing, is the database's to refuse (PostgreSQL does).
- `aggregate()` over the groups raises `QueryError` — the subtotal rows would count every row again.
- PostgreSQL has them (`Features.supports_grouping_sets`); on SQLite and ClickHouse a grouping set and
  `Grouping()` raise `UnSupportedError` before the query is sent. One `group_by()` takes one grouping set —
  `QueryError` otherwise; `Rollup()`/`Cube()` without fields, a `GroupingSets()` naming no field and
  `Grouping()` without fields raise `QueryError`.

## <a id="window-functions"></a>Window functions

```python
class Window(CombinableExpression):
    def __init__(
        self,
        expression: WindowFunction | Aggregate,
        partition_by: Sequence[str | Expression] = (),
        order_by: Sequence[str | Expression | Ordering] = (),
        frame: WindowFrame | None = None,
    ) -> None: ...
```

`expression` can also be an aggregate of `hare.query.functions` — `Window(Sum("price"))`,
`Window(Count("id"), partition_by=["author_id"])`, `Window(Avg(F("price") * 2))`; its `_filter=` becomes
the window aggregate's `FILTER (WHERE ...)` — an empty `Q()` (also `~Q()`, `Q(Q())`) keeps every row, as
for a plain aggregate. `distinct=True` and an aggregate without a window counterpart (`ArrayAgg`,
`StringAgg`, ...) raise `QueryError`. A window takes arithmetic like any expression:
`Window(RowNumber(), order_by=["id"]) + 1`, `Window(Sum("fee"), order_by=["lent_at"]) - F("fee")`.

`order_by` uses the same `-field` convention as `QuerySet.order_by()`, and also accepts
`F("field").asc()`/`.desc()` orderings with `nulls_first=True`/`nulls_last=True` to fix where `NULL`s
sort inside the window (see [NULL ordering](queryset-methods.md#null-ordering)). `partition_by` and
`order_by` also take expressions — `partition_by=[Coalesce("genre", Value("none"))]`,
`order_by=[(F("price") * F("quantity")).desc()]`.

The window functions are in `hare.query.functions.window` —
`from hare.query.functions.window import Lag, Rank, RowNumber`. Its
`Sum`/`Avg`/`Max`/`Min`/`Count`/`StdDev`/`Variance` are the window twins of the aggregates; `condition=`
is their `FILTER (WHERE ...)`, like an aggregate's `_filter=`.

| Class | Signature | SQL |
|---|---|---|
| `RowNumber` | `()` | `ROW_NUMBER()` |
| `Rank` | `()` | `RANK()` |
| `DenseRank` | `()` | `DENSE_RANK()` |
| `NTile` | `(buckets: int)` | `NTILE(buckets)`; `buckets` an integer from 1 to 2147483647, otherwise `QueryError` |
| `Sum`/`Avg`/`Max`/`Min`/`Count` | <code>(field: str &#124; Expression, condition: Q &#124; None = None)</code> | `SUM`/`AVG`/`MAX`/`MIN`/`COUNT(field)` |
| `StdDev`/`Variance` | <code>(field: str &#124; Expression, condition: Q &#124; None = None, sample: bool = False)</code> | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP(field)` |
| `CumeDist`/`PercentRank` | `()` | `CUME_DIST()`/`PERCENT_RANK()` |
| `FirstValue`/`LastValue` | <code>(field: str &#124; Expression, condition: Q &#124; None = None)</code> | `FIRST_VALUE`/`LAST_VALUE(field)` |
| `NthValue` | <code>(field: str &#124; Expression, nth: int = 1)</code> | `NTH_VALUE(field, nth)` over the whole partition, like `LastValue`; `nth` from 1 to 2147483647, otherwise `QueryError` |
| `Lag`/`Lead` | <code>(field: str &#124; Expression, offset: int = 1, default: Any = None)</code> | `LAG`/`LEAD(field, offset, default)`; `offset` an integer from 0 to 2147483647, otherwise `QueryError` |

```python
await Book.objects.all().annotate(
    price_rank=Window(Rank(), partition_by=["author_id"], order_by=["-price"]),
    previous_price=Window(Lag("price"), partition_by=["author_id"], order_by=["published_at"]),
)
```

Result types: `Count` is always an `int` (and a `Sum`/`Max`/`Min` over a `Count()` annotation is too, `Avg`
over it a `float`); `Avg` of integers is what the database computes: an exact `Decimal` on PostgreSQL, whose
`numeric` average a `float` would cut, a `float` on SQLite and ClickHouse, also inside arithmetic or `Concat`;
`Avg` of a `FloatField` is a `float`, of a `DecimalField` what the database computes: on PostgreSQL a
`Decimal` that is not rounded to the field's `decimal_places` (the numeric average at its own division scale —
`1.7125`, not `1.71`, for a `DecimalField(decimal_places=2)`), on ClickHouse a `float`
(`Features.computes_decimal_statistics_exactly`), the same in `.aggregate()`, `.annotate()` and a window; over
no rows, `Min`/`Max`/`Avg`/`StdDev`/`Variance` give `None` on every database (but `Min`/`Max` of a
ClickHouse array, map or tuple give its [empty value](../dialects/clickhouse/types.md#containers)), and `Sum` gives `None` on
PostgreSQL and SQLite but ClickHouse's own `0` (`0.00` for a Decimal, `None` for a nullable field);
`Sum`/`Max`/`Min`/`FirstValue`/`LastValue`/`Lag`/`Lead` keep the type of what they aggregate, including an
expression or literal annotation (`Sum(F("duration") * 2)` is a `timedelta`, a window `Sum` over
`Value(Decimal("1.5"))` a `Decimal`). A `Lag`/`Lead` `default` is encoded through the field like a value
written to it (a `dict` for a `JSONField`, a `datetime`/`Decimal` for its field); a default the field's type
can't hold without loss (`Lag("pages", 1, 0.5)`) raises `QueryError`. `StdDev`/`Variance` type their result
like `Avg`; `CumeDist`/`PercentRank` are a `float` from 0 to 1, `NthValue` keeps the field's type.

For a `GeneratedField`, the `Lag`/`Lead` default must fit its `output_field` under the same rules.

> [!NOTE]
> **`LastValue` always sees the whole partition**
>
> `LastValue(field)` renders an explicit `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`
> frame, so it always returns the field's value on the partition's actual last row. Without an explicit
> frame, SQL's own default frame when `order_by` is set
> (`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`) would make `LAST_VALUE` always equal the
> *current* row's value instead — a well-known SQL pitfall. `FirstValue` isn't affected by this (the
> default frame's lower bound is already `UNBOUNDED PRECEDING`), so it keeps SQL's default frame. A
> `frame=` given replaces it.

### <a id="window-frames"></a>Frames

`frame=` sets the rows around the current one an aggregate or a value function (`FirstValue`,
`LastValue`, `NthValue`) computes over — `RowRange` and `ValueRange` from `hare.query.expressions`:

```python
from hare.query.expressions import RowRange, ValueRange, Window

await Loan.objects.annotate(
    # The current loan and the two before it.
    moving_sum=Window(Sum("fee"), order_by=["lent_at"], frame=RowRange(start=-2, end=0)),
    # Every loan whose fee is at most 10 below or above the current one's.
    nearby=Window(Count("id"), order_by=["fee"], frame=ValueRange(start=-10, end=10)),
)
```

`start` and `end` are offsets from the current row: a negative number before it (`n PRECEDING`), 0 the
current row (`CURRENT ROW`), a positive number after it (`n FOLLOWING`), `None` unbounded — from the
partition's first row for `start`, to its last for `end`; both default to `None`.

- `RowRange` — `ROWS BETWEEN ...`: the offsets count rows.
- `ValueRange` — `RANGE BETWEEN ...`: the offsets measure the ordering's value, and rows of the same
  value (peers) are in each other's frame — `ValueRange(start=None, end=0)` is SQL's default frame with
  `order_by`. An offset other than 0 needs the window ordered by exactly one numeric field; another
  `order_by` raises `QueryError`.

A frame that holds no row gives `NULL` (`RowRange(start=1, end=2)` on the partition's last row). A
ranking function (`RowNumber`, `Rank`, `DenseRank`, `NTile`, `CumeDist`, `PercentRank`) and `Lag`/`Lead`
always see the whole partition — a frame raises `QueryError`; so does an offset that isn't an `int` or
`None`, or a `start` after `end`. Frames work the same on PostgreSQL, SQLite and ClickHouse.

> [!WARNING]
> **Filtering on a `Window(...)` annotation**
>
> SQL evaluates window functions *after* `WHERE`/`GROUP BY`/`HAVING`, so a window function can never
> appear in the same query's `WHERE`. A `.values()`/`.values_list()` query filtered on one —
> `ranked_qs.filter(price_rank=1).values("id", "title")`, also inside `Q(...) | Q(...)`, `exclude()`,
> compared with a field (`filter(previous_price__lt=F("price"))`) or with the window on the value side
> (`filter(price__gt=F("previous_price"))`) — is built as a derived table: the inner query keeps the
> other filters, the grouping and the window functions (a filter on a regular field applies before the
> window is computed), the outer one filters on the window's column and applies the ordering,
> `distinct()`, `limit()`/`offset()` and a keyset cursor to the rows left; a `with_cte()` stays at the
> top of the statement. Combined with `select_for_update()` it raises `QueryError`. A queryset of model
> instances (and its `count()`/`exists()`/`update()`/`delete()`) filtered on a window function raises
> `QueryError` — filter a `.values()` query instead, e.g.
> `Book.objects.filter(pk__in=Subquery(ranked_qs.filter(price_rank=1).values("pk")))`. A window in a
> `Case(When(...))` condition is valid SQL and works anywhere; `.aggregate(Sum("price_rank"))` over a
> window annotation and `.update(field=Window(...))` raise `QueryError`.
