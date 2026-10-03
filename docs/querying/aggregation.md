# Aggregates, grouping and window functions

## Implicit `GROUP BY` {: #implicit-group-by }

The implicit `GROUP BY` of an aggregate `annotate()` follows Django's rules:

- An expression that contains an aggregate anywhere - `Count("books") + F("id")`,
  `Coalesce(Sum("books__pages"), F("fallback"))`, `F("book_count") * F("price")` over an aggregate
  annotation, a `Case` with an aggregate in one branch - is an aggregate expression: its filters go to
  `HAVING`, and the query is grouped by the columns it reads outside the aggregate.
- A selected non-aggregate expression is grouped by its `SELECT` position (`GROUP BY 2`), so a literal
  inside it (`F("price") + 5`, `Case(When(..., then=Value("high")))`, `Concat(...)`) binds once.
- A column `HAVING` reads outside its aggregates (`Q(book_count__gte=2) | Q(city="Oslo")`,
  `book_count__gt=F("limit")`) is grouped too.
- A correlated `Exists(...)`/`Subquery(...)` next to an aggregate is grouped as a whole, by its `SELECT`
  position (like Django 4.2+), not by the outer columns its `OuterRef(...)` reads - two outer rows with
  different `OuterRef` values but the same subquery result land in one group. Only a subquery reading an
  outer aggregate is grouped by those columns.
- A window function (`Window(...)`) is never a group key - it is computed per group.
- A queryset of model instances (and its `count()`/`exists()`/`update()`/`delete()`) is always grouped by
  the primary key as well - one group per row. `values()`/`values_list()` group by exactly the fields named
  an aggregate annotated **after** them (`values("author_id").annotate(n=Count("id"))` - one row per
  author); an aggregate annotated **before** them, or passed to them as a keyword argument, is computed per
  model row, grouped by the primary key too (`annotate(n=Count("books")).values("country", "n")` - one row
  per author), like Django.

## Aggregates and to-many filters {: #aggregates-to-many }

`annotate(n=Count("products")).filter(products__price__gt=6)` reuses the aggregate's own `JOIN`, so the
count covers only the products matching the filter — exactly as `filter(...).annotate(...)` does, whichever
order you call them in (unlike Django, where the order changes the result). A non-`distinct` aggregate
is correct only while its own relation chain is the only to-many `JOIN` of the query: a second to-many
relation — another aggregate, a deeper hop such as `Count("products__reviews")` next to `Sum("products__price")`,
the aggregate's own `_filter=Q(orders__...)`, a `.filter(orders__...)`, a non-aggregate annotation such as `F("orders__total")`/`Case(When(orders__...))`, a `values("orders__total")` field or an `.order_by("orders__total")` — repeats each row per related row
and would silently inflate the numbers, so it raises `QueryError` instead; pass `distinct=True` to every
affected `Count()` (safe for a count of the relation's rows), move the aggregate into a `Subquery()`
annotation, or run the aggregates as separate queries — `distinct=True` doesn't fix `Sum()`/`Avg()`, which
would then drop equal values rather than repeated rows (`.exclude(orders__...)` is fine — it becomes a
`NOT EXISTS` subquery, not a `JOIN`). An `OuterRef("orders__id")` inside a `Subquery()`/`Exists()` joins
`orders` into the outer query and counts the same way. `Max()`/`Min()` (and `BoolAnd()`/`BoolOr()`) can't be
changed by a repeated row, so they need no `distinct=True`. An aggregate over the base table's own columns (`Sum("budget")`,
`Count("id")`) has no to-many chain of its own, so any to-many `JOIN` of the query — another aggregate's, a
`.filter(products__...)`'s, an annotation's, a `values()` field's or the ordering's — raises the same `QueryError`; `Count("id", distinct=True)` is fine, and so is
`.distinct().aggregate(...)` or an `aggregate()` over an aggregate annotation, which run over one row per base row.

A to-many field used as a `GROUP BY` key splits the rows into groups instead of repeating them inside one:
`.annotate(total=Sum("qty")).group_by("tags__name").values_list("tags__name", "total")` (or the implicit
grouping of `.values("tags__name").annotate(total=Sum("qty"))`) sums each tag's products, as
in Django. A base row counts once per related row carrying the key value - grouping departments by
`emps__active` counts a department once per active employee, so count the departments themselves with
`Count("id", distinct=True)`. A key that is the relation's primary key or a non-nullable unique field
(`tags__id`), or keys covering every field of a non-nullable `UniqueConstraint` or composite primary key of
the related model, pin one related row per group (a nullable unique field doesn't - its NULL rows share one
group - unless the relation's first `.filter()` call rules NULL out on that same `JOIN`, e.g. `tags__code__isnull=False`
or `tags__code="x"`; `tags__code__in=["x", None]` keeps NULL, and a negated condition, `~~Q(...)` too, is a subquery
that rules nothing out on the `JOIN`), so an aggregate over another to-many relation is correct next to it, as in Django:
`values("tags__id", "tags__name").annotate(n=Count("reviews"))` counts each tag's reviews. Any other key
(`tags__name`) splits the rows only while no non-`distinct` aggregate reads another to-many relation: two tags
may share a name, and each repeats the reviews within that group, so `Count("reviews")` grouped by
`tags__name` alone raises - add the primary key to the key. Only the key's own `JOIN` splits the rows: a
second `.filter(tags__...)` call builds a separate `JOIN` over `tags`, which repeats the rows inside each
group, so it raises too - put the conditions into one `.filter()` call. A plain
`F("tags__name")` annotation or `.alias()` used as the key counts as that field. Grouping by an aggregate
annotation raises `FieldError`.

A to-many `JOIN` a filter narrows to at most one related row per row repeats nothing, so an aggregate next
to it is exact, as in Django: an equality with one value on the related primary key, on a unique field
(nullable too - an equality never matches `NULL`) or on every field of a unique set - `filter(tags=tag)`,
`filter(tags=1)`, `filter(tags__pk=1)`, `filter(tags__id=1)`, `filter(tags__slug="x")`,
`filter(tags__group=1, tags__rank=2)` - ANDed at the top of a `.filter()` call, or `__isnull=True` of a
reverse FK (`filter(books__isnull=True)`, also on its primary key or a non-nullable field), which leaves one
`NULL` row. `Book.objects.filter(tags=1).aggregate(total=Sum("price"))`,
`Author.objects.filter(books=1).annotate(n=Count("awards"))`, a sliced `aggregate()`, keyset `iterator()` and
`Subquery(Book.objects.filter(tags=OuterRef("pk")).values("id").annotate(n=Count("id")))` all work. The condition
narrows only the `JOIN` its own `.filter()` call reads (the first call over the relation reads the one
`values()`/`order_by()`/aggregates share; a later call builds its own), and only its last to-many hop
(`filter(books__tags=1)` still repeats each book). A many-to-many link counts only when the through table
can't link two rows twice - the automatic through table's unique index, or a unique set of a `through=`
model covering both keys. Anything else - `__in`, a range, a non-unique field, part of a unique set, an
`OR`, a many-to-many `__isnull`, `__isnull=False` - still raises. So does an aggregate over the narrowed
relation itself next to another to-many `JOIN` (`filter(books=1).annotate(Sum("books__pages"),
Count("awards"))`): the one book still repeats once per award - Django returns the inflated sum there.

## Window functions {: #window-functions }

```python
class Window(Expression):
    def __init__(self, expression: WindowFunction | Aggregate, partition_by: Sequence[str] = (), order_by: Sequence[str | Ordering] = ()) -> None
```

Like Django, `expression` can also be an aggregate of `hare.query.functions` - `Window(Sum("salary"))`,
`Window(Count("id"), partition_by=["department_id"])`, `Window(Avg(F("salary") * 12))`; its `_filter=` becomes the
window aggregate's `FILTER (WHERE ...)` - an empty `Q()` (also `~Q()`, `Q(Q())`) keeps every row, as for a plain
aggregate. `distinct=True` and an aggregate without a window counterpart
(`ArrayAgg`, `StringAgg`, ...) raise `QueryError`.

`order_by` uses the same `-field` convention as `QuerySet.order_by()`, and also accepts
`F("field").asc()`/`.desc()` orderings with `nulls_first=True`/`nulls_last=True` to fix where
`NULL`s sort inside the window (see [NULL ordering](queryset-methods.md#null-ordering)).

| Class | Signature | SQL |
|---|---|---|
| `RowNumber` | `()` | `ROW_NUMBER()` |
| `Rank` | `()` | `RANK()` |
| `DenseRank` | `()` | `DENSE_RANK()` |
| `NTile` | `(buckets: int)` | `NTILE(buckets)` |
| `Sum`/`Avg`/`Max`/`Min`/`Count` | `(field: str)` | `SUM`/`AVG`/`MAX`/`MIN`/`COUNT(field)` |
| `StdDev`/`Variance` | `(field: str, sample: bool = False)` | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP(field)` |
| `CumeDist`/`PercentRank` | `()` | `CUME_DIST()`/`PERCENT_RANK()` |
| `FirstValue`/`LastValue` | `(field: str)` | `FIRST_VALUE`/`LAST_VALUE(field)` |
| `NthValue` | `(field: str, nth: int = 1)` | `NTH_VALUE(field, nth)` over the whole partition, like `LastValue`; `nth` from 1, otherwise `QueryError` |
| `Lag`/`Lead` | `(field: str, offset: int = 1, default: Any = None)` | `LAG`/`LEAD(field, offset, default)` |

```python
await Employee.objects.all().annotate(
    salary_rank=Window(Rank(), partition_by=["department_id"], order_by=["-salary"]),
    prev_salary=Window(Lag("salary"), partition_by=["department_id"], order_by=["hired_at"]),
)
```

Result types: `Count` is always an `int` (and a `Sum`/`Max`/`Min` over a `Count()`
annotation is too, `Avg` over it a `float`); `Avg` of integers is a `float` on every backend, also
inside arithmetic or `Concat`; `Avg` of a `FloatField` is a `float`, of a `DecimalField` a `Decimal`
that is not rounded to the field's `decimal_places` (precision over dialect parity: Postgres returns
its exact numeric average, SQLite the nearest `Decimal` of its double - `1.7125`, not `1.71`, for a
`DecimalField(decimal_places=2)`), the same in `.aggregate()`, `.annotate()` and a window; `Sum`/`Max`/
`Min`/`FirstValue`/`LastValue`/`Lag`/`Lead` keep the type of what they aggregate, including an
expression or literal annotation (`Sum(F("duration") * 2)` is an `int`, a window `Sum` over
`Value(Decimal("1.5"))` a `Decimal`). A `Lag`/`Lead` `default` is encoded through the field like a
value written to it (a `dict` for a `JSONField`, a `datetime`/`Decimal` for its field); a default
the field's type can't hold without loss (`Lag("int_field", 1, 0.5)`) raises `QueryError`.
`StdDev`/`Variance` type their result like `Avg`; `CumeDist`/`PercentRank` are a `float` from 0 to 1,
`NthValue` keeps the field's type.

!!! note "`LastValue` always sees the whole partition"
    `LastValue(field)` renders an explicit `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED
    FOLLOWING` frame, so it always returns the field's value on the partition's actual last row.
    Without an explicit frame, SQL's own default frame when `order_by` is set (`RANGE BETWEEN
    UNBOUNDED PRECEDING AND CURRENT ROW`) would make `LAST_VALUE` always equal the *current* row's
    value instead — a well-known SQL pitfall. `FirstValue` isn't affected by this (the default
    frame's lower bound is already `UNBOUNDED PRECEDING`), so it keeps SQL's default frame.

!!! warning "Filtering on a `Window(...)` annotation"
    SQL evaluates window functions *after* `WHERE`/`GROUP BY`/`HAVING`, so a window function can
    never appear in the same query's `WHERE`. A `.values()`/`.values_list()` query filtered on
    one - `ranked_qs.filter(salary_rank=1).values("id", "name")`, also inside `Q(...) | Q(...)`,
    `exclude()`, compared with a field (`filter(prev_salary__lt=F("salary"))`) or with the window
    on the value side (`filter(salary__gt=F("prev_salary"))`) - is built as a derived table: the
    inner query keeps the other filters, the grouping and the window functions (a filter on a
    regular field applies before the window is computed, like in Django), the outer one filters on
    the window's column and applies the ordering, `distinct()`, `limit()`/`offset()` and a keyset
    cursor to the rows left; a `with_cte()` stays at the top of the statement. Combined with
    `select_for_update()` it raises `QueryError`. A queryset of model instances (and its
    `count()`/`exists()`/`update()`/`delete()`) filtered on a window function raises
    `QueryError` - filter a `.values()` query instead, e.g.
    `Employee.objects.filter(pk__in=Subquery(ranked_qs.filter(salary_rank=1).values("id")))` (`"id"`, not
    `"pk"` - a literal `"pk"` never resolves in `.values()`/`.values_list()`). A window in a
    `Case(When(...))` condition is valid SQL and works anywhere; `.aggregate(Sum("salary_rank"))`
    over a window annotation and `.update(field=Window(...))` raise `QueryError`.
