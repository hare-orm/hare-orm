# `values()` and `values_list()`

`.values()` and `.values_list()` return a full queryset of dicts / tuples (flat values, namedtuples),
like Django's `QuerySet` after `values()`. Every chained method applies the same `.values()` call to
the chained queryset — `qs.values("a").filter(x=1)` is exactly `qs.filter(x=1).values("a")` — so all
queryset semantics (slicing rules, `HAVING`, cursors, ambient tenant/soft-delete scope, connection
choice) carry over. Chained methods use field and annotation names, as on the queryset; an expression
passed to `values(name=expression)`/`values_list(name=expression)` is readable by that name too, and so
is a renamed field: `values(title="name").filter(title="a").order_by("-title")` (a new name that is a field
of the model keeps meaning that field).

| Method | Type | What it does |
|---|---|---|
| `filter()` / `exclude()` / `order_by()` / `distinct(*fields)` / `alias()` / `group_by()` / `all()` / `none()` / `limit()` / `offset()` / `using()` / `select_for_update()` / `with_cte()` / `after_cursor()` / `before_cursor()` / `all_tenants()` / `include_deleted()` / `only_deleted()` | chain | Same as on the queryset, keeping the selected fields and the row shape. A filter on an aggregate annotation applies to the groups (`HAVING`). |
| `annotate(**kwargs)` | chain | Adds the annotations to the selected columns (after them). An aggregate groups the rows by the other selected columns, like Django: `Book.objects.all().values("author_id").annotate(n=Count("id"))` returns one row per author. The order matters, as in Django: an aggregate annotated **before** `values()`/`values_list()` (also by `.alias()`), or passed to them as a keyword argument, is computed per model row — `Author.objects.annotate(n=Count("books")).values("country", "n")` returns one row per author, grouped by the primary key too. A `.filter(books__...)` chained after `values(...).annotate(n=Count("books"))` reuses the aggregate's `JOIN`, so the count covers only the matching books — unlike Django, which adds a second `JOIN` there and inflates the count (see [Aggregates and to-many filters](aggregation.md#aggregates-to-many)). A `flat=True` `values_list()` keeps its one column — the annotation still groups and filters it: `values_list("author__name", flat=True).annotate(n=Count("id")).filter(n__gt=3)`. Without selected fields (`values()`), the annotation joins every field. |
| `values(...)` / `values_list(...)` | chain | Replaces the selected fields (dicts ↔ tuples too), like Django. A query an aggregate groups by the selected fields stays grouped by them: `values("author_id").annotate(n=Count("id")).values_list("n", flat=True)` is one count per author. |
| `qs[a:b]` / `qs[i]` | chain / terminal | A slice composes with any slice already taken; an index is the one row at that position — `IndexError` when awaited if there is none; a negative index raises `QueryError`. |
| `count()` | await | The number of rows `await qs` returns — of groups for a grouped query, of distinct rows for a `.distinct()` one, the slice included — counted over the query as a derived table. |
| `exists()` | await | Whether `await qs` returns any row, the slice included. |
| `aggregate(**kwargs)` | await | When every row is a row of the source queryset (no grouping, no `.distinct()` over the selected fields, no slice, no selected field or ordering across a to-many relation), this is the queryset's own `aggregate()` and can read any field. Otherwise it runs over the rows the query returns, as a derived table, and can read only the selected columns — by output name, or by the field name a renamed key selects; anything else raises `QueryError`, an annotation a `flat=True` `values_list()` doesn't select included. `values("author_id").annotate(n=Count("id")).aggregate(most=Max("n"))`, `values_list("rating", flat=True).distinct().aggregate(total=Sum("rating"))`, `order_by("-rating")[:3].aggregate(...)`. A query reading a window function aggregates over the derived table too. |
| `first()` / `last()` | terminal | The first/last row in the ordering; unordered, a grouped query is ordered by its group key (the selected non-aggregate fields, or the `.group_by()` fields), any other by the primary key. |
| `earliest(*fields)` / `latest(*fields)` / `get(*args, does_not_exist_exception=..., multiple_objects_returned_exception=..., **kwargs)` | terminal | As on the queryset, returning a row. On a sliced query whose rows aren't one per model row (grouped, `.distinct()`, or multiplied by a to-many relation), these and `last()` raise `QueryError` — the slice is taken over rows the queryset can't narrow to. |
| `iterator(chunk_size=1000)` | async generator | Pages through the rows. Without an `order_by()`, pages follow `Meta.ordering`, else the group key of a grouped query, else the primary key; the columns making the ordering unique per row are appended — the group key of a grouped query, the selected fields of a `.distinct()` one, otherwise the primary key (when the ordering isn't unique already) and every selected field across a to-many relation. Pages by keyset (safe under concurrent deletes) when every ordering column is a selected field of the model itself, otherwise by `OFFSET`. The query's own slice and `after_cursor()` bound the whole iteration. |
| `stream(chunk_size=1000)` | async generator | A cursor, like [`QuerySet.stream()`](queryset-methods.md#stream) — inside a transaction. |
| `union(*others, all=False)` / `intersection(*others)` / `difference(*others)` | chain | A queryset of the combined rows — see below. |
| `update(**kwargs)` | await | Updates the rows of the source queryset, like Django — the selected fields don't matter. `QueryError` on a grouped query (its rows are groups, not model rows) and on a sliced one whose rows aren't one per model row. |
| `delete()` / `contains()` / `only()` / `defer()` / `select_related()` / `prefetch_related()` | — | `QueryError` — call them on the queryset before `.values()`. |
| `sql()` / `explain()` | sync / `async def` | As on the queryset. |

**`.distinct()` ordered by a field it doesn't select.** `SELECT DISTINCT` has to select every
`ORDER BY` column, so Django deduplicates over the ordering columns too and can return a selected value
several times. hare returns each distinct combination of the selected columns once instead — its first
row in the ordering, ordered by where that row comes — computed in SQL (`ROW_NUMBER() OVER (PARTITION BY
<selected columns> ORDER BY <ordering>)`), sliced after deduplication, and usable as a subquery
(`id__in=...`), sliced or not:

```python
# ratings 5 "sci", 4 "art", 3 "sci", 3 None, 3 "art", ...
await Book.objects.all().order_by("-rating").distinct().values_list("subject", flat=True)  # ["sci", "art", None]
```

**Set operations.** `union()`/`intersection()`/`difference()` of `.values()`/`.values_list()` queries
return a queryset of the combined rows. Every branch selects the same number of columns (`QueryError` otherwise);
the rows take the first branch's shape (dicts, tuples, flat values, namedtuples) and names, and each
column decodes through the first branch that knows its field. A branch may be ordered, sliced or itself
a set operation (it is combined as a derived table); branches may come from different models. Chained
calls combine each new branch with the whole result so far, as for model querysets. Such a union
supports `order_by()` by output names, `limit()`/`offset()`/slices/index, `count()`, `exists()`,
`first()`/`last()` (unordered: by every output column), `get()`, `aggregate()` (reading output names),
`iterator()` (`OFFSET` paging, every output column as a tie-breaker), `stream()`, `sql()`/`explain()`, and
works as an `__in` value: `Author.objects.filter(id__in=a.values_list("author_id", flat=True).union(b...))`.
`filter()`/`exclude()`/`annotate()`/`update()`/`delete()` on it raise `QueryError` — apply them to the
branches. `.values(...)`/`.values_list(...)` on a union of model querysets apply the call to every branch, keeping the
union's ordering (by fields the rows select) and slice. Combining a model queryset with a values query
raises `QueryError`.

## <a id="relation-field-paths"></a>Relations and `pk` in field paths

As in Django, a field path may end on a relation or on `pk`, everywhere a field path is accepted —
`values()`/`values_list()`, `order_by()`,
`group_by()`, `distinct(<fields>)`, `F()` and aggregates:

- a forward FK/O2O name (`values("author")`) reads its own key column (`author_id`) with no `JOIN` — the
  value stays even when the related row is hidden by its default scope (tenant, soft delete,
  `Meta.manager`), like the column itself; `only("author")` loads that column;
- a to-many relation (`values("tags")`, `values("books")`) or a reverse one-to-one reads the related
  primary key — a row per related row, `NULL` for a row without one — through the same `JOIN` as
  `values("tags__id")`, scoped by the related model's default scope;
- a trailing `pk` (`values("tags__pk")`, `values("author__pk")`, `order_by("tags__pk")`,
  `group_by("author__pk")`, `only("pk")`) reads the primary key field of the model it reaches — through
  the `JOIN`, so `author__pk` is `NULL` for a hidden author while `author` keeps the column value.

The output keeps the name you passed (`{"tags": 1}`, `row.tags` of a `named=True` row). A key read this
way works like any selected field: `values("tags").annotate(n=Count("id"))` groups by the tag (a row per
tag, one `NULL` group for the rows without a tag — the key is the primary key, so an aggregate over
another to-many relation is correct next to it), and it combines with `.distinct()`, `order_by()`,
`count()`, `aggregate()`, `iterator()`, `union()` and `__in` subqueries
(`Tag.objects.filter(id__in=Book.objects.filter(...).values("tags"))`). A composite primary key (or a relation to one) is
read as a tuple of its components — `values("pk")`, `values_list("bay", flat=True)` give `(1, 2)`, and
`None` when every component is `NULL`; `group_by()`, `order_by()` (`"pk"`/`"-pk"` and `Meta.ordering`
too) and `distinct(<fields>)` use every component. `Count("bay")` counts the rows with a key,
`Count("bay", distinct=True)` the distinct keys, never a `NULL` one. `F()` of such a relation raises
`QueryError` naming its components.

## <a id="date-part-paths"></a>Date parts in field paths

Like Django's transforms, a field path may end on a part of a date, time or datetime field — in
`values()`/`values_list()`, `order_by()`, `group_by()` and `distinct(<fields>)`:

```python
await Event.objects.all().values("created__year")
await Event.objects.all().values_list("starts__hour", "created__date")
await Event.objects.all().values(hour="created__hour")
await Event.objects.all().order_by("-created__month")
await Event.objects.all().order_by("created__date").distinct("created__date")  # DISTINCT ON on PostgreSQL, ROW_NUMBER() elsewhere
await Event.objects.annotate(count=Count("id")).group_by("created__year").values("created__year", "count")
```

| Part | Value |
|---|---|
| `year`, `iso_year`, `quarter`, `month`, `week`, `day` | an integer |
| `week_day` | 1 = Sunday ... 7 = Saturday |
| `iso_week_day` | 1 = Monday ... 7 = Sunday |
| `hour`, `minute`, `second`, `microsecond` | an integer |
| `date`, `time` | a datetime's date / time of day |

A `DatetimeField` has every part. A `DateField` has only the calendar parts (`year` ... `day`), a
`TimeField` only the time-of-day ones (`hour` ... `microsecond`); a part the field doesn't have
raises `FieldError`. A path may go through relations first: `tournament__created__year`. A
datetime's part is taken in the current zone — `Timezone.override()`, else the configured one — as
the `__year`/`__date` lookups take it; under `use_timezone=True` a `time` comes back with the zone's
offset, as a `TimeField` value does.

`distinct()` takes an annotation's name as well:
`.annotate(year=ExtractYear("created")).distinct("year").order_by("year", "id")`. On PostgreSQL the
zone of an `Extract()` is written into the SQL as a literal, so one part in `SELECT`, `ORDER BY` and
`DISTINCT ON` is the same expression; a filter (`created__hour=...`) still binds the zone as a
parameter.
