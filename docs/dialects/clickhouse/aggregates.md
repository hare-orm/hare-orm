# ClickHouse aggregates

`hare.dialects.clickhouse.functions.aggregates` has ClickHouse's own aggregates. They run in
`annotate()`, `aggregate()` and `Window(...)` like hare's, and take `_filter=` and `distinct=True`
as any aggregate does.

```python
from hare.dialects.clickhouse.functions.aggregates import ArgMax, Quantile, TopK, Uniq

await Event.objects.values("day").annotate(visitors=Uniq("user_id"))
await Quote.objects.values("symbol").annotate(last_price=ArgMax("price", "quoted_at"))
await Order.objects.aggregate(typical=Quantile("total", 0.5), high=Quantile("total", 0.99, exact=True))
await Event.objects.aggregate(popular=TopK("page", 5))
```

| Aggregate | SQL | Result |
|---|---|---|
| `Uniq(field)` | `uniq` | About how many distinct values a group has — an estimate from a sample of hashes, the fastest |
| `UniqExact(field)` | `uniqExact` | How many distinct values, exactly — every one kept in memory |
| `UniqCombined(field)` | `uniqCombined` | An estimate in less memory and closer than `Uniq` for many values |
| `Quantile(field, level=0.5, *, exact=False)` | `quantile(level)`, `quantileExact(level)` | The value below which `level` of the values lie — an interpolated estimate, or with `exact=True` one of the values |
| `Quantiles(field, *levels, exact=False)` | `quantiles(...)`, `quantilesExact(...)` | Several quantiles in one pass, an array in the order of the levels |
| `Median(field, *, exact=False)` | `quantile(0.5)` | The median — an estimate, or the middle value with `exact=True` |
| `ArgMin(field, by)`, `ArgMax(field, by)` | `argMin`, `argMax` | The value of `field` in the row of the group where `by` is the least or the greatest |
| `GroupArray(field, *, max_size=None)` | `groupArray` | The values as an array in the order read, `NULL` left out; `max_size` keeps the first that many |
| `GroupUniqArray(field, *, max_size=None)` | `groupUniqArray` | The distinct values as an array, in no order; `max_size` keeps that many of them |
| `TopK(field, k=10)` | `topK(k)` | About the `k` most frequent values, the most frequent first — exact only for few distinct values |
| `AnyValue(field)`, `AnyLast(field)` | `any`, `anyLast` | The first or the last value ClickHouse meets in the group, whichever it is |
| `SumMap(field)` | `sumMap` | The maps of a `MapField` added up key by key |

`Quantiles`, `GroupArray`, `GroupUniqArray` and `TopK` give a list; `SumMap` a dict. `Uniq`,
`Quantile` and `TopK` are estimates and may differ between runs over many rows — `UniqExact` and
`exact=True` are deterministic.

## <a id="filter-distinct-and-empty-groups"></a>`_filter`, `distinct` and empty groups

- `_filter=Q(...)` is written `FILTER (WHERE ...)` and `distinct=True` as `DISTINCT` inside the
  call, as on other databases — ClickHouse runs them as its `-If` and `-Distinct` combinators.
- Over no rows `Sum`, `Max`, `Min`, `Avg`, `StdDev` and `Variance` give `None` as in SQL: they are
  written by their `-OrNull` form (`sumOrNull(amount)`). In a grouped statement, whose groups hold at
  least one row, an aggregate without `_filter` keeps its plain, cheaper form. A sample deviation or
  variance over one value is `None`, not ClickHouse's NaN.
- `ArrayAgg` of hare is `groupArray` of a tuple holding each value — it keeps the `NULL` values a plain
  `groupArray` leaves out, as `ARRAY_AGG` does on PostgreSQL; with `distinct=True` it is
  `groupUniqArray`, and its `order_by` sorts the array.
